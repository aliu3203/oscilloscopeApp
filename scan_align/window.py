"""Mini app for centering the PMQ doublet by scanning one stage axis at a time.

1. Locate: capture with the wire off-center; find both steps on each diode and lock their times.
2. Scan: pick the stage and axis, put the stage at a position, Collect (one capture). Each magnet's
   kick (its step) on both diodes is plotted against position. Move to the next position, Collect
   again. From two positions on, each magnet's kick is fitted with a line, and where the target
   magnet's line crosses zero is the position to move to.
   The cage stage centers PMQ2 (it moves both magnets); the PMQ1 mount then centers PMQ1.

Talks to the scope only through the main app's local API (ScopeClient), so the main app must be
open and connected. Connecting to the scope directly would kick the app off.
"""

import time
from dataclasses import dataclass

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QSettings, Qt, QThreadPool
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from desktop.main_window import CHANNEL_COLORS
from desktop.worker import Worker
from scan_align.session import Session
from scan_align.zero import ScanPoint, analyze, decimate
from scope.client import ScopeClient
from scope.siglent import CHANNELS
from translate_align.alignment import MAGNETS, step_vectors
from translate_align.steps import locate_steps, measure_steps

# Photodiode roles -> default channel. The diodes sense roughly x and y; which channel is which varies by setup.
DIODES = {"Diode X": "C2", "Diode Y": "C3"}
# Which magnet's wave reaches the diodes first depends on which side of the cage the diodes are. On this bench it is PMQ2's.
DEFAULT_FIRST = "PMQ2"
# Stage -> (name, magnet it centers). The cage moves both magnets, so it centers PMQ2; the mount moves PMQ1 only.
STAGES = {"cage": ("Cage stage", "PMQ2"), "mount": ("PMQ1 mount", "PMQ1")}
AXES = ("x", "y")
UNITS = ("µm", "mm", "ticks", "turns")
MAGNET_COLORS = {"PMQ1": (31, 119, 180), "PMQ2": (214, 39, 40)}


@dataclass
class Shot:
    """One capture of both diodes and each diode's step fit."""

    raw: dict  # channel -> Trace as captured (saved to disk)
    traces: dict  # channel -> Trace, block-averaged for fitting
    models: dict  # role -> StepModel used
    fits: dict  # role -> StepFit
    trigger_mode: str


def shoot(client, channels, models=None):
    """Worker thread: one capture of both diodes, locate the steps if no models, fit both.

    Exactly one capture per call: back-to-back captures can catch the scope without a finished
    acquisition, and the read then times out and leaves it stopped.
    """
    status = client.status()
    if not status["connected"]:
        raise RuntimeError("The capture app is not connected to the scope: Connect it there, then try again")
    off = [ch for ch in channels.values() if ch not in status["enabled_channels"]]
    if off:
        raise RuntimeError(f"{', '.join(off)} off on the scope: turn it on, then try again")
    raw = client.capture(list(channels.values()))
    traces = {ch: decimate(t) for ch, t in raw.items()}
    if models is None:
        models = {role: locate_steps(traces[ch]) for role, ch in channels.items()}
    fits = {role: measure_steps(traces[ch], models[role]) for role, ch in channels.items()}
    return Shot(raw, traces, models, fits, status["trigger_mode"])


class ScanWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Scan Align")
        self.resize(1250, 900)

        self.client = ScopeClient()
        self.session = Session()
        self.settings = QSettings("researchMusumeci", "ScanAlign")
        self._worker = None  # keep a reference so its signals outlive the pool's run()
        self.models = None  # role -> StepModel, locked by Locate
        self.channels = None  # role -> channel the models were located on
        self.scan = 1
        self.points = []  # (position, Shot) per Collect, in the order taken
        self.last_shot = None
        self._pending_x = None

        # Connection row
        self.refresh_btn = QPushButton("Refresh")
        self.conn_label = QLabel("Not checked")
        conn_row = QHBoxLayout()
        conn_row.addWidget(QLabel(f"App API: {self.client.url}"))
        conn_row.addWidget(self.refresh_btn)
        conn_row.addWidget(self.conn_label)
        conn_row.addStretch()
        conn_row.addWidget(QLabel(f"Saving to {self.session.dir}"))

        # Photodiodes
        self.diode_boxes = {}
        diodes_layout = QFormLayout()
        for role, default in DIODES.items():
            box = QComboBox()
            box.addItems(CHANNELS)
            box.setCurrentText(self.settings.value(f"channel/{role}", default))
            box.currentTextChanged.connect(self.on_channel_changed)
            self.diode_boxes[role] = box
            diodes_layout.addRow(role, box)
        self.first_box = QComboBox()
        self.first_box.addItems(MAGNETS)
        self.first_box.setCurrentText(self.settings.value("first_to_arrive", DEFAULT_FIRST))
        self.first_box.currentTextChanged.connect(self.on_order_changed)
        diodes_layout.addRow("First step to arrive", self.first_box)
        diodes_group = QGroupBox("Photodiodes")
        diodes_group.setLayout(diodes_layout)

        # 1. Locate
        self.locate_btn = QPushButton("Capture && locate steps")
        self.locate_label = QLabel("Wire should be visibly off-center so both steps show on both diodes.")
        self.locate_label.setWordWrap(True)
        locate_layout = QVBoxLayout()
        locate_layout.addWidget(self.locate_label)
        locate_layout.addWidget(self.locate_btn)
        locate_group = QGroupBox("1. Locate steps")
        locate_group.setLayout(locate_layout)

        # 2. Scan
        self.stage_box = QComboBox()
        for key, (name, magnet) in STAGES.items():
            self.stage_box.addItem(f"{name} (centers {magnet})", key)
        self.stage_box.setCurrentIndex(max(0, self.stage_box.findData(self.settings.value("stage", "cage"))))
        self.axis_box = QComboBox()
        self.axis_box.addItems(AXES)
        self.axis_box.setCurrentText(self.settings.value("axis", "x"))
        self.unit_box = QComboBox()
        self.unit_box.setEditable(True)
        self.unit_box.addItems(UNITS)
        self.unit_box.setCurrentText(self.settings.value("unit", UNITS[0]))
        self.step_spin = QDoubleSpinBox()
        self.step_spin.setRange(-1e5, 1e5)
        self.step_spin.setDecimals(3)
        self.step_spin.setValue(float(self.settings.value("step", 20.0)))
        self.next_spin = QDoubleSpinBox()
        self.next_spin.setRange(-1e6, 1e6)
        self.next_spin.setDecimals(3)
        self.next_spin.setValue(float(self.settings.value("next", 0.0)))
        self.scan_label = QLabel()
        self.scan_label.setWordWrap(True)
        self.collect_btn = QPushButton("Collect")
        self.undo_btn = QPushButton("Undo last")
        self.new_btn = QPushButton("New scan")
        scan_form = QFormLayout()
        scan_form.addRow("Stage", self.stage_box)
        scan_form.addRow("Axis", self.axis_box)
        scan_form.addRow("Unit", self.unit_box)
        scan_form.addRow("Step Δ", self.step_spin)
        scan_form.addRow("Next position", self.next_spin)
        scan_buttons = QHBoxLayout()
        scan_buttons.addWidget(self.undo_btn)
        scan_buttons.addWidget(self.new_btn)
        scan_layout = QVBoxLayout()
        scan_layout.addLayout(scan_form)
        scan_layout.addWidget(self.scan_label)
        scan_layout.addWidget(self.collect_btn)
        scan_layout.addLayout(scan_buttons)
        scan_group = QGroupBox("2. Scan")
        scan_group.setLayout(scan_layout)

        # Result
        self.result_label = QLabel("Collect at least 2 positions.")
        self.result_label.setWordWrap(True)
        self.result_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        font = QFont(self.result_label.font())
        font.setPointSize(font.pointSize() + 2)
        self.result_label.setFont(font)
        result_layout = QVBoxLayout()
        result_layout.addWidget(self.result_label)
        result_group = QGroupBox("Zero crossing")
        result_group.setLayout(result_layout)

        left_panel = QVBoxLayout()
        for group in (diodes_group, locate_group, scan_group, result_group):
            left_panel.addWidget(group)
        left_panel.addStretch()
        left_widget = QWidget()
        left_widget.setLayout(left_panel)
        left_widget.setFixedWidth(340)

        # Plots: kick vs position for each diode (sharing x), then the last capture, above the results log
        self.plots = pg.GraphicsLayoutWidget()
        self.plots.setBackground("w")
        self.kick_plots = {}
        for row, role in enumerate(DIODES):
            plot = self.plots.addPlot(row=row, col=0)
            plot.showGrid(x=True, y=True)
            plot.setLabel("left", "kick (step, mV)")
            plot.addLegend(offset=(10, 10))
            plot.getAxis("left").enableAutoSIPrefix(False)  # keep mV as mV
            self.kick_plots[role] = plot
        self.kick_plots["Diode Y"].setXLink(self.kick_plots["Diode X"])
        self.trace_plot = self.plots.addPlot(row=len(DIODES), col=0)
        self.trace_plot.showGrid(x=True, y=True)
        self.trace_plot.setLabel("left", "mV")
        self.trace_plot.setLabel("bottom", "time after trigger (µs)")
        self.trace_plot.setTitle("Last capture: data with the pickup tone removed, and the step fit")
        self.trace_plot.addLegend(offset=(10, 10))
        self.trace_plot.getAxis("left").enableAutoSIPrefix(False)
        self.results = QPlainTextEdit()
        self.results.setReadOnly(True)
        self.results.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))

        right = QSplitter(Qt.Vertical)
        right.addWidget(self.plots)
        right.addWidget(self.results)
        right.setSizes([660, 200])

        body = QHBoxLayout()
        body.addWidget(left_widget)
        body.addWidget(right, stretch=1)
        root = QVBoxLayout()
        root.addLayout(conn_row)
        root.addLayout(body)
        central = QWidget()
        central.setLayout(root)
        self.setCentralWidget(central)

        self.refresh_btn.clicked.connect(self.on_refresh_clicked)
        self.locate_btn.clicked.connect(self.on_locate_clicked)
        self.collect_btn.clicked.connect(self.on_collect_clicked)
        self.undo_btn.clicked.connect(self.on_undo_clicked)
        self.new_btn.clicked.connect(self.on_new_scan)
        self.stage_box.currentIndexChanged.connect(lambda _: self._on_scan_setting("stage", self.stage_box.currentData()))
        self.axis_box.currentTextChanged.connect(lambda axis: self._on_scan_setting("axis", axis))
        self.step_spin.valueChanged.connect(lambda step: self.settings.setValue("step", step))
        self.next_spin.valueChanged.connect(lambda x: self.settings.setValue("next", x))
        self.unit_box.currentTextChanged.connect(self.on_unit_changed)

        self.on_unit_changed(self.unit_box.currentText())
        self._set_busy(False)
        self.statusBar().showMessage("Start the Oscilloscope Capture app and connect it, then Refresh")

    # --- helpers ------------------------------------------------------------

    def _run(self, fn, on_finished, *args, **kwargs):
        self._worker = Worker(fn, *args, **kwargs)
        self._worker.signals.finished.connect(on_finished)
        self._worker.signals.failed.connect(self.on_failed)
        QThreadPool.globalInstance().start(self._worker)

    def _set_busy(self, busy, message=None):
        located = self.models is not None
        scanning = bool(self.points)
        self.refresh_btn.setEnabled(not busy)
        self.locate_btn.setEnabled(not busy)
        for box in self.diode_boxes.values():
            box.setEnabled(not busy and not scanning)  # the scan's kicks are on these channels
        self.first_box.setEnabled(not busy)
        for widget in (self.stage_box, self.axis_box):
            widget.setEnabled(not busy and not scanning)  # one scan is one stage axis
        for widget in (self.unit_box, self.step_spin, self.next_spin):
            widget.setEnabled(not busy)
        self.collect_btn.setEnabled(not busy and located)
        self.undo_btn.setEnabled(not busy and scanning)
        self.new_btn.setEnabled(not busy and scanning)
        self._update_scan_label()
        if message:
            self.statusBar().showMessage(message)

    def _stage(self):
        return self.stage_box.currentData()

    def _stage_name(self):
        return STAGES[self._stage()][0]

    def _target(self):
        return STAGES[self._stage()][1]

    def _axis(self):
        return self.axis_box.currentText()

    def _unit(self):
        return self.unit_box.currentText().strip() or "units"

    def _x(self, x):
        return f"{x:+.5g} {self._unit()}"

    def _order(self):
        """Magnets in the order their steps arrive at the diodes."""
        first = self.first_box.currentText()
        return (first, *(m for m in MAGNETS if m != first))

    def _diode_channels(self):
        return {role: box.currentText() for role, box in self.diode_boxes.items()}

    def _steps(self, shot):
        return step_vectors(shot.fits, list(DIODES), self._order())

    def _report(self, text):
        self.results.appendPlainText(text)
        self.results.verticalScrollBar().setValue(self.results.verticalScrollBar().maximum())

    def _update_scan_label(self):
        what = f"Scan {self.scan}: {self._stage_name()} {self._axis()}"
        if self.models is None:
            self.scan_label.setText(f"{what}. Locate the steps first.")
        elif not self.points:
            self.scan_label.setText(f"{what}. Set Next position to the stage reading, then Collect. Scan across "
                                    f"where {self._target()}'s kick changes sign if you can.")
        else:
            self.scan_label.setText(f"{what}: {len(self.points)} point(s), last at {self._x(self.points[-1][0])}. "
                                    "Move the stage to Next position, then Collect.")

    def _warn_trigger(self, shot):
        if shot.trigger_mode == "AUTO":
            self._report("  ! Scope is in AUTO trigger, so a capture can be one without a pulse. Use NORMAL trigger on the pulse.")

    def on_failed(self, message):
        self._set_busy(False, message)
        self._report(f"! {message}")

    def on_unit_changed(self, unit):
        self.settings.setValue("unit", unit)
        for spin in (self.step_spin, self.next_spin):
            spin.setSuffix(f" {self._unit()}")
        self._redraw()

    def _on_scan_setting(self, key, value):
        self.settings.setValue(key, value)
        self._redraw()
        self._set_busy(False)

    def on_channel_changed(self, _):
        channels = self._diode_channels()
        for role, channel in channels.items():
            self.settings.setValue(f"channel/{role}", channel)
        if self.models is not None and channels != self.channels:
            self.models = None
            self.locate_label.setText("Diode channels changed: locate the steps again.")
            self._report("Diode channels changed: step lock cleared.")
            self._set_busy(False)

    def on_order_changed(self, first):
        """The kicks are re-read from each point's fits, so the plots just relabel."""
        self.settings.setValue("first_to_arrive", first)
        self._report(f"First step to arrive is now {first}: kicks relabelled.")
        if self.last_shot is not None:
            self._plot_trace(self.last_shot)
        self._redraw(report=bool(self.points))

    # --- refresh ------------------------------------------------------------

    def on_refresh_clicked(self):
        self._set_busy(True, "Checking app…")
        self._run(self.client.status, self.on_status)

    def on_status(self, status):
        self._set_busy(False)
        if not status["connected"]:
            self.conn_label.setText("App running, scope not connected")
            self.statusBar().showMessage("Connect the scope in the Oscilloscope Capture app")
            return
        enabled = status["enabled_channels"]
        self.conn_label.setText(f"Connected: {status['idn']}")
        self.statusBar().showMessage(f"Trigger mode: {status['trigger_mode']}  channels on: {', '.join(enabled)}")
        # Prefer enabled channels in the pickers; keep the current choice if it is still offered.
        for box in self.diode_boxes.values():
            current = box.currentText()
            box.blockSignals(True)
            box.clear()
            box.addItems(enabled or CHANNELS)
            box.setCurrentText(current)
            box.blockSignals(False)
        self.on_channel_changed(None)

    # --- 1. locate ----------------------------------------------------------

    def on_locate_clicked(self):
        channels = self._diode_channels()
        if len(set(channels.values())) != len(channels):
            self.statusBar().showMessage("Pick a different channel for each diode")
            return
        self._set_busy(True, "Capturing and locating…")
        self._run(shoot, self.on_located, self.client, channels, None)

    def on_located(self, shot):
        self.channels, self.models, self.last_shot = self._diode_channels(), shot.models, shot
        file = self.session.save_capture("locate", shot.raw)
        steps = self._steps(shot)
        self.session.log({"kind": "locate", "channels": self.channels, "file": file, "models": shot.models, "steps": steps})
        first, second = self._order()
        lines = [f"\n[{time.strftime('%H:%M:%S')}] Located steps"]
        for role, m in shot.models.items():
            lines.append(f"  {role} ({self.channels[role]}): {first} at {m.t1:.0f} µs (edge {m.w1:.0f} µs), "
                         f"{second} at {m.t2:.0f} µs (edge {m.w2:.0f} µs)")
        self._report("\n".join(lines))
        self._warn_trigger(shot)
        self.locate_label.setText("Locked: " + "; ".join(f"{self.channels[r]} {m.t1:.0f}/{m.t2:.0f} µs"
                                                         for r, m in self.models.items()))
        if self.points:
            # Re-measure the scan so far with the new step times, so every point uses the same ones.
            self.points = [(x, Shot(s.raw, s.traces, self.models,
                                    {r: measure_steps(s.traces[ch], self.models[r]) for r, ch in self.channels.items()},
                                    s.trigger_mode))
                           for x, s in self.points]
            self._report("  The scan so far was re-measured with the new step times.")
        self._plot_trace(shot)
        self._redraw(report=bool(self.points))
        self._set_busy(False, "Steps located and locked. Next: scan a stage axis.")

    # --- 2. scan ------------------------------------------------------------

    def on_collect_clicked(self):
        self._pending_x = self.next_spin.value()
        self._set_busy(True, f"Capturing at {self._stage_name()} {self._axis()} = {self._x(self._pending_x)}…")
        self._run(shoot, self.on_collected, self.client, self.channels, self.models)

    def on_collected(self, shot):
        x = self._pending_x
        self.points.append((x, shot))
        self.last_shot = shot
        k = len(self.points)
        file = self.session.save_capture(f"scan{self.scan:02d}_p{k:03d}", shot.raw)
        steps = self._steps(shot)
        self.session.log({"kind": "point", "scan": self.scan, "stage": self._stage(), "axis": self._axis(),
                          "unit": self._unit(), "x": x, "steps": steps, "file": file, "trigger_mode": shot.trigger_mode,
                          "fit_noise_uv": {r: f.resid_uv for r, f in shot.fits.items()}})
        self._save_scan()

        roles = list(DIODES)
        kicks = "; ".join(f"{m} " + ", ".join(f"{self.channels[r]} {steps[m][0][i]:+.3f}±{steps[m][1][i]:.3f}"
                                              for i, r in enumerate(roles)) for m in MAGNETS)
        self._report(f"[{time.strftime('%H:%M:%S')}] {self._stage_name()} {self._axis()} = {self._x(x)}: {kicks} mV")
        self._warn_trigger(shot)
        self.next_spin.setValue(x + self.step_spin.value())
        self._plot_trace(shot)
        self._redraw(report=True)
        self._set_busy(False, f"Point {k} collected. Move to {self._x(self.next_spin.value())} and Collect.")

    def on_undo_clicked(self):
        x, _ = self.points.pop()
        self.next_spin.setValue(x)
        self._save_scan()
        self.session.log({"kind": "undo", "scan": self.scan, "x": x})
        self._report(f"Removed the point at {self._x(x)}.")
        self._redraw(report=True)
        self._set_busy(False, "Last point removed")

    def on_new_scan(self):
        self.scan += 1
        self.points = []
        self._report(f"\n--- Scan {self.scan} ---")
        self._redraw()
        self._set_busy(False, f"Scan {self.scan}: pick the stage and axis, set Next position, and Collect")

    def _scan_points(self):
        return [ScanPoint(x, self._steps(shot)) for x, shot in self.points]

    def _save_scan(self):
        self.session.save_scan(self.scan, self._stage(), self._axis(), self._unit(), list(DIODES), self._scan_points())

    # --- display ------------------------------------------------------------

    def _plot_trace(self, shot):
        """Both diodes' last capture, the fitted steps, and where each step sits."""
        plot = self.trace_plot
        plot.clear()
        for role, fit in shot.fits.items():
            channel, model = self.channels[role], shot.models[role]
            plot.plot(fit.t, fit.clean, pen=pg.mkPen((180, 180, 180), width=1))
            plot.plot(fit.t, fit.model, pen=pg.mkPen(CHANNEL_COLORS.get(channel, "#333333"), width=2.5),
                      name=f"{role} ({channel})")
            for t0, magnet in zip((model.t1, model.t2), self._order()):
                plot.addItem(pg.InfiniteLine(t0, pen=pg.mkPen(MAGNET_COLORS[magnet], style=Qt.DotLine), label=magnet,
                                             labelOpts={"position": 0.9, "color": MAGNET_COLORS[magnet]}))
        plot.autoRange()

    def _redraw(self, report=False):
        """Kick vs. position for each magnet on each diode, the fitted lines, and the target's zero crossing."""
        unit, target = self._unit(), self._target()
        for role, plot in self.kick_plots.items():
            plot.clear()
            channel = (self.channels or self._diode_channels())[role]
            plot.setTitle(f"Scan {self.scan}: {role} ({channel}) kick vs. {self._stage_name()} {self._axis()}")
            plot.setLabel("bottom", f"{self._stage_name()} {self._axis()} ({unit})")
            plot.addItem(pg.InfiniteLine(0, angle=0, pen=pg.mkPen((90, 90, 90), width=1.5)))
        if not self.points:
            self.result_label.setText("Collect at least 2 positions.")
            return

        points = self._scan_points()
        x = np.array([p.x for p in points])
        try:
            result = analyze(points, list(DIODES), target)
        except ValueError as e:
            result = None
            self.result_label.setText(str(e))

        for k, (role, plot) in enumerate(self.kick_plots.items()):
            for magnet in MAGNETS:
                color, is_target = MAGNET_COLORS[magnet], magnet == target
                S = np.array([p.steps[magnet][0][k] for p in points])
                E = np.array([p.steps[magnet][1][k] for p in points])
                plot.addItem(pg.ErrorBarItem(x=x, y=S, height=2 * E, pen=pg.mkPen(color)))
                plot.plot(x, S, pen=None, symbol="o" if is_target else "t", symbolSize=9 if is_target else 7,
                          symbolBrush=color if is_target else None, symbolPen=pg.mkPen(color, width=1.5),
                          name=f"{magnet}{' (target)' if is_target else ''}")
                if result is None:
                    continue
                fit = result.fits[magnet][role]
                if not np.isfinite(fit.x0):
                    continue
                lo, hi = x.min(), x.max()
                if abs(fit.x0 - (lo + hi) / 2) < 3 * (hi - lo):
                    lo, hi = min(lo, fit.x0), max(hi, fit.x0)  # draw the line out to its zero crossing
                xs = np.array([lo, hi])
                plot.plot(xs, fit.slope * (xs - fit.x0), pen=pg.mkPen(color, width=2 if is_target else 1.2,
                                                                      style=Qt.SolidLine if is_target else Qt.DashLine))
            if result is not None and result.role is not None:
                plot.addItem(pg.InfiniteLine(result.x0, pen=pg.mkPen((20, 20, 20), width=1.5, style=Qt.DashLine),
                                             label=f"move here: {result.x0:.4g}",
                                             labelOpts={"position": 0.08, "color": (20, 20, 20)}))
        for plot in self.kick_plots.values():
            plot.autoRange()
        if result is not None:
            self._show_result(result, report)

    def _show_result(self, result, report):
        unit, last = self._unit(), self.points[-1][0]
        stage, axis, target = self._stage_name(), self._axis(), result.target
        if result.role is None:
            text = "No zero crossing yet."
        else:
            best = result.fits[target][result.role]
            text = (f"Move the {stage} {axis} to {self._x(result.x0)} ± {result.x0_err:.2g}\n"
                    f"({result.x0 - last:+.4g} {unit} from the last point at {self._x(last)})\n"
                    f"Centers {target} along {axis}. From {result.role} ({self.channels[result.role]}): "
                    f"slope {best.slope:+.4g} mV/{unit}.")
            text += self._other_magnet_text(result)
        notes = list(result.notes)
        if self._stage() == "mount" and self._mount_moves_pmq2(result):
            notes.append("PMQ2's kick changes more than PMQ1's on this mount scan, but the mount only moves PMQ1: "
                         "the arrival order looks swapped. Change 'First step to arrive'.")
        if notes:
            text += "\n\n" + "\n".join(f"! {note}" for note in notes)
        self.result_label.setText(text)
        if not report:
            return
        self._report("  " + text.replace("\n\n", "\n").replace("\n", "\n  "))
        self.session.log({"kind": "analysis", "scan": self.scan, "stage": self._stage(), "axis": axis, "unit": unit,
                          "target": target, "role": result.role, "x0": result.x0, "x0_err": result.x0_err,
                          "fits": result.fits, "notes": notes})

    def _other_magnet_text(self, result):
        """Where the other magnet's kick crosses zero on this scan, when it says something useful."""
        other = next(m for m in MAGNETS if m != result.target)
        usable = {r: f for r, f in result.fits[other].items() if f.significant}
        if self._stage() != "cage" or not usable:
            return ""
        role = min(usable, key=lambda r: usable[r].x0_err)
        f, unit = usable[role], self._unit()
        gap = f.x0 - result.x0
        return (f"\n{other}'s kick crosses zero at {self._x(f.x0)} ± {f.x0_err:.2g}. With the cage at "
                f"{result.target}'s zero, the PMQ1 mount has to move PMQ1 by {gap:+.4g} {unit} along the cage's "
                f"+{self._axis()} direction.")

    @staticmethod
    def _mount_moves_pmq2(result):
        steepest = lambda magnet: max(abs(f.slope) for f in result.fits[magnet].values())  # noqa: E731
        return steepest("PMQ2") > steepest("PMQ1")
