"""Mini app for centering the PMQ doublet on the wire.

1. Locate: capture with the wire off-center; find both steps on each diode and lock their times.
   This capture is also the home point that calibration and the first recommendation start from.
2. Calibrate: capture +x and +y of home on the cage stage (and optionally the PMQ1 mount) to
   measure how each step responds to each stage.
3. Measure & recommend: capture, measure both steps, and get the cage and PMQ1-mount moves that
   null them. Make the moves and repeat until the steps are within noise.

Talks to the scope only through the main app's local API (ScopeClient), so the main app must be
open and connected. Connecting to the scope directly would kick the app off.
"""

import time
from dataclasses import dataclass

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QSettings, Qt, QThreadPool
from PySide6.QtGui import QFontDatabase
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
from scope.client import ScopeClient
from scope.siglent import CHANNELS
from translate_align.alignment import MAGNETS, coupling, fit_calibration, recommend, step_vectors
from translate_align.session import Session
from translate_align.steps import locate_steps, measure_steps

# Photodiode roles -> default channel. The diodes sense roughly x and y; which channel is which varies by setup.
DIODES = {"Diode X": "C2", "Diode Y": "C3"}
# Which magnet's wave reaches the diodes first depends on which side of the cage the diodes are.
# On this bench PMQ2's arrives first: a cage move that nulled the second step centered PMQ1.
DEFAULT_FIRST = "PMQ2"
STAGES = {"cage": "Cage stage", "mount": "PMQ1 mount"}
CAL_BUMPS = ("+x", "+y")  # captured after the home point, which is the Locate capture


@dataclass
class Point:
    """One measurement position: the capture and each diode's step fit."""

    traces: dict  # channel -> Trace, both diodes from the same acquisition
    models: dict  # role -> StepModel used
    fits: dict  # role -> StepFit
    trigger_mode: str


def acquire(client, channels, models=None):
    """Worker thread: one capture of both diodes, locate the steps if no models, fit both."""
    status = client.status()
    if not status["connected"]:
        raise RuntimeError("The capture app is not connected to the scope: Connect it there, then try again")
    traces = client.capture(list(channels.values()))
    if models is None:
        models = {role: locate_steps(traces[ch]) for role, ch in channels.items()}
    fits = {role: measure_steps(traces[ch], models[role]) for role, ch in channels.items()}
    return Point(traces, models, fits, status["trigger_mode"])


def _vec(v, unit, digits=3):
    return f"x {v[0]:+.{digits}f}, y {v[1]:+.{digits}f} {unit}"


class AlignWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Translate Align")
        self.resize(1250, 820)

        self.client = ScopeClient()
        self.session = Session()
        self.settings = QSettings("researchMusumeci", "TranslateAlign")
        self._worker = None  # keep a reference so its signals outlive the pool's run()
        self.models = None  # role -> StepModel, locked by Locate
        self.channels = None  # role -> channel the models were located on
        self.calibrations = {}  # "cage" / "mount" -> Calibration
        self.cal_stage = None  # stage being calibrated, None when idle
        self.cal_offsets, self.cal_steps = [], []
        self.home_steps = None  # steps from the Locate capture: the home point for calibration

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
        setup_group = QGroupBox("Photodiodes")
        setup_group.setLayout(diodes_layout)

        # 1. Locate
        self.locate_btn = QPushButton("Capture && locate steps")
        self.locate_label = QLabel("Wire should be visibly off-center so both steps show on both diodes.")
        self.locate_label.setWordWrap(True)
        locate_layout = QVBoxLayout()
        locate_layout.addWidget(self.locate_label)
        locate_layout.addWidget(self.locate_btn)
        locate_group = QGroupBox("1. Locate steps")
        locate_group.setLayout(locate_layout)

        # 2. Calibrate
        self.stage_box = QComboBox()
        for key, name in STAGES.items():
            self.stage_box.addItem(name, key)
        self.bump_spin = QDoubleSpinBox()
        self.bump_spin.setRange(5, 500)
        self.bump_spin.setSingleStep(10)
        self.bump_spin.setSuffix(" µm")
        self.bump_spin.setValue(float(self.settings.value("bump", 50.0)))
        self.cal_label = QLabel()
        self.cal_label.setWordWrap(True)
        self.cal_btn = QPushButton("Capture point")
        self.cal_cancel_btn = QPushButton("Cancel")
        cal_form = QFormLayout()
        cal_form.addRow("Stage", self.stage_box)
        cal_form.addRow("Bump", self.bump_spin)
        cal_buttons = QHBoxLayout()
        cal_buttons.addWidget(self.cal_btn)
        cal_buttons.addWidget(self.cal_cancel_btn)
        cal_layout = QVBoxLayout()
        cal_layout.addLayout(cal_form)
        cal_layout.addWidget(self.cal_label)
        cal_layout.addLayout(cal_buttons)
        cal_group = QGroupBox("2. Calibrate")
        cal_group.setLayout(cal_layout)

        # 3. Measure
        self.measure_btn = QPushButton("Capture && recommend")
        measure_layout = QVBoxLayout()
        measure_layout.addWidget(self.measure_btn)
        measure_group = QGroupBox("3. Measure && recommend")
        measure_group.setLayout(measure_layout)

        left_panel = QVBoxLayout()
        for group in (setup_group, locate_group, cal_group, measure_group):
            left_panel.addWidget(group)
        left_panel.addStretch()
        left_widget = QWidget()
        left_widget.setLayout(left_panel)
        left_widget.setFixedWidth(330)

        # Plots (one per diode) above the results log
        self.plots = pg.GraphicsLayoutWidget()
        self.plots.setBackground("w")
        self.plot_items = {}
        for row, role in enumerate(DIODES):
            plot = self.plots.addPlot(row=row, col=0)
            plot.showGrid(x=True, y=True)
            plot.setLabel("left", "mV")
            plot.setLabel("bottom", "time after trigger (µs)")
            plot.addLegend(offset=(10, 10))
            self.plot_items[role] = plot
        self.results = QPlainTextEdit()
        self.results.setReadOnly(True)
        self.results.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))

        right = QSplitter(Qt.Vertical)
        right.addWidget(self.plots)
        right.addWidget(self.results)
        right.setSizes([480, 300])

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
        self.cal_btn.clicked.connect(self.on_cal_clicked)
        self.cal_cancel_btn.clicked.connect(self.on_cal_cancel)
        self.measure_btn.clicked.connect(self.on_measure_clicked)
        self.bump_spin.valueChanged.connect(lambda value: self.settings.setValue("bump", value))

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
        calibrating = self.cal_stage is not None
        self.refresh_btn.setEnabled(not busy)
        self.locate_btn.setEnabled(not busy and not calibrating)
        for widget in (*self.diode_boxes.values(), self.first_box):
            widget.setEnabled(not busy and not calibrating)
        self.stage_box.setEnabled(not busy and located and not calibrating)
        self.bump_spin.setEnabled(not busy and located and not calibrating)
        self.cal_btn.setEnabled(not busy and located)
        self.cal_cancel_btn.setEnabled(not busy and calibrating)
        self.measure_btn.setEnabled(not busy and located and not calibrating)
        self._update_cal_label()
        if message:
            self.statusBar().showMessage(message)

    def _order(self):
        """Magnets in the order their steps arrive at the diodes."""
        first = self.first_box.currentText()
        return (first, *(m for m in MAGNETS if m != first))

    def _diode_channels(self):
        return {role: box.currentText() for role, box in self.diode_boxes.items()}

    def _acquire(self, on_finished, models):
        self._set_busy(True, "Capturing…")
        self._run(acquire, on_finished, self.client, self.channels, models)

    def _report(self, text):
        self.results.appendPlainText(text)
        self.results.verticalScrollBar().setValue(self.results.verticalScrollBar().maximum())

    def on_failed(self, message):
        self._set_busy(False, message)
        self._report(f"! {message}")

    def on_channel_changed(self, _):
        channels = self._diode_channels()
        for role, channel in channels.items():
            self.settings.setValue(f"channel/{role}", channel)
        if self.models is not None and channels != self.channels:
            self.models, self.calibrations = None, {}
            self.locate_label.setText("Diode channels changed: locate the steps again.")
            self._report("Diode channels changed: step lock and calibrations cleared.")
            self._set_busy(False)

    def on_order_changed(self, first):
        """Relabel the steps already measured, so the home point and calibrations stay usable."""
        self.settings.setValue("first_to_arrive", first)
        swap = lambda d: {"PMQ1": d["PMQ2"], "PMQ2": d["PMQ1"]}  # noqa: E731
        if self.home_steps is not None:
            self.home_steps = swap(self.home_steps)
        for cal in self.calibrations.values():
            cal.J, cal.S0 = swap(cal.J), swap(cal.S0)
        self._report(f"First step to arrive is now {first}: home point and calibrations relabelled.")

    # --- display ------------------------------------------------------------

    def _plot(self, point):
        """Per diode: tone-removed data, the fitted steps, and where each step sits."""
        for role, plot in self.plot_items.items():
            plot.clear()
            fit, model = point.fits[role], point.models[role]
            channel = self.channels[role]
            plot.setTitle(f"{role} ({channel})")
            plot.plot(fit.t, fit.clean, pen=pg.mkPen((170, 170, 170), width=1), name="data, tone removed")
            plot.plot(fit.t, fit.model, pen=pg.mkPen(CHANNEL_COLORS[channel], width=2.5), name="fit")
            for t0, magnet in zip((model.t1, model.t2), self._order()):
                line = pg.InfiniteLine(t0, pen=pg.mkPen((60, 60, 60), style=Qt.DotLine), label=magnet,
                                       labelOpts={"position": 0.92, "color": (60, 60, 60)})
                plot.addItem(line)
            plot.autoRange()

    def _steps_text(self, steps):
        roles = list(DIODES)
        header = "              " + "".join(f"{f'{r} ({self.channels[r]})':>22}" for r in roles)
        lines = [header]
        for magnet in MAGNETS:
            S, E = steps[magnet]
            lines.append(f"  {magnet} step  " + "".join(f"{f'{s:+.3f} ± {e:.3f} mV':>22}" for s, e in zip(S, E)))
        return "\n".join(lines)

    def _point_header(self, title, point):
        noise = ", ".join(f"{self.channels[r]} {f.resid_uv:.0f}" for r, f in point.fits.items())
        lines = [f"\n[{time.strftime('%H:%M:%S')}] {title}: fit noise {noise} µV"]
        if point.trigger_mode == "AUTO":
            lines.append("  ! Scope is in AUTO trigger, so a capture can be one without a pulse. Use NORMAL trigger on the pulse.")
        return "\n".join(lines)

    def _log(self, kind, label, point, steps, **extra):
        file = self.session.save_point(label, point.traces)
        self.session.log({"kind": kind, "label": label, "channels": self.channels, "file": file,
                          "models": point.models, "steps": steps, **extra})

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
        if self.calibrations:
            self._report("Re-locating: step times change, so earlier calibrations were cleared.")
        self.channels, self.models, self.calibrations = channels, None, {}
        self._acquire(self.on_located, None)

    def on_located(self, point):
        self.models = point.models
        steps = step_vectors(point.fits, list(DIODES), self._order())
        self._plot(point)
        lines = [self._point_header("Located steps", point)]
        first, second = self._order()
        for role, m in point.models.items():
            lines.append(f"  {role} ({self.channels[role]}): {first} at {m.t1:.0f} µs (edge {m.w1:.0f} µs), "
                         f"{second} at {m.t2:.0f} µs (edge {m.w2:.0f} µs)")
        lines.append(self._steps_text(steps))
        self._report("\n".join(lines))
        self.home_steps = steps
        self.locate_label.setText("Locked: " + "; ".join(f"{self.channels[r]} {m.t1:.0f}/{m.t2:.0f} µs" for r, m in self.models.items())
                                  + ". This capture is home: note your stage readings.")
        self._log("locate", "locate", point, steps)
        self._report("  This capture is the home point. Note the cage stage and PMQ1 mount readings now.")
        self._set_busy(False, "Steps located and locked. Next: calibrate the cage stage.")

    # --- 2. calibrate -------------------------------------------------------

    def _update_cal_label(self):
        if self.cal_stage is None:
            done = ", ".join(STAGES[k] for k in self.calibrations) or "none"
            self.cal_label.setText(f"Calibrated: {done}. Home is the Locate capture. To calibrate, move the chosen "
                                   "stage +x from home by the bump, then Capture point.")
            return
        stage, bump, i = STAGES[self.cal_stage], self.cal_bump, len(self.cal_offsets) - 1
        self.cal_label.setText((
            f"Point 1/2: move the {stage} +{bump:.0f} µm in x from home, then Capture point.",
            f"Point 2/2: move the {stage} back to home x, then +{bump:.0f} µm in y, then Capture point.",
        )[i])

    def on_cal_clicked(self):
        if self.cal_stage is None:
            self.cal_stage, self.cal_bump = self.stage_box.currentData(), self.bump_spin.value()
            self.cal_offsets, self.cal_steps = [(0.0, 0.0)], [self.home_steps]
        self._acquire(self.on_cal_point, self.models)

    def on_cal_cancel(self):
        self.cal_stage = None
        self._set_busy(False, "Calibration cancelled")

    def on_cal_point(self, point):
        i, bump = len(self.cal_offsets) - 1, self.cal_bump
        offset = ((bump, 0.0), (0.0, bump))[i]
        steps = step_vectors(point.fits, list(DIODES), self._order())
        self.cal_offsets.append(offset)
        self.cal_steps.append(steps)
        label = f"{self.cal_stage}_{CAL_BUMPS[i]}"
        self._plot(point)
        self._report(self._point_header(f"Calibration {STAGES[self.cal_stage]} {CAL_BUMPS[i]}", point) + "\n" + self._steps_text(steps))
        self._log("calibration point", label, point, steps, stage=self.cal_stage, offset_um=offset)
        if i + 1 < len(CAL_BUMPS):
            self._set_busy(False, f"Calibration point {i + 1}/{len(CAL_BUMPS)} done")
            return

        stage = self.cal_stage
        self.cal_stage = None
        cal = fit_calibration(stage, self.cal_offsets, self.cal_steps, bump)
        self.calibrations[stage] = cal
        self._report(self._calibration_text(cal))
        self.session.log({"kind": "calibration", "calibration": cal})
        if stage == "cage":
            self._report("Return the cage stage to home. Recommendation from home (the Locate capture):")
            self._recommend(self.home_steps)
        self._set_busy(False, f"{STAGES[stage]} calibrated")

    def _calibration_text(self, cal):
        lines = [f"  {STAGES[cal.stage]} response (mV per µm; columns = stage x, y; rows = diode X, Y):"]
        for magnet in MAGNETS:
            J = cal.J[magnet]
            lines.append(f"    {magnet}: [{J[0, 0]:+.5f} {J[0, 1]:+.5f}; {J[1, 0]:+.5f} {J[1, 1]:+.5f}]   cond {np.linalg.cond(J):.1f}")
        if cal.stage == "mount":
            if self._order_looks_swapped():
                lines.append("    ! The step labelled PMQ2 is the one that moves with the PMQ1 mount: the arrival order "
                             "looks swapped. Change 'First step to arrive' (calibrations are relabelled for you).")
            if "cage" in self.calibrations:
                c = coupling(cal, self.calibrations["cage"])
                lines.append(f"    PMQ2 response to the mount is {c:.0%} of its response to the cage (ideally ~0)")
        return "\n".join(lines)

    # --- 3. measure & recommend ---------------------------------------------

    def on_measure_clicked(self):
        self._acquire(self.on_measured, self.models)

    def on_measured(self, point):
        steps = step_vectors(point.fits, list(DIODES), self._order())
        self._plot(point)
        self._report(self._point_header("Measure", point) + "\n" + self._steps_text(steps))
        rec = None
        if "cage" in self.calibrations:
            rec = self._recommend(steps)
        else:
            self._report("  Calibrate the cage stage to get recommended moves.")
        self._log("measure", "measure", point, steps, recommendation=rec)
        self._set_busy(False, "Measured")

    def _order_looks_swapped(self):
        """The mount moves PMQ1 only, so the step that responds to it is PMQ1's."""
        mount = self.calibrations.get("mount")
        return mount is not None and np.linalg.norm(mount.J["PMQ2"]) > np.linalg.norm(mount.J["PMQ1"])

    def _recommend(self, steps):
        if self._order_looks_swapped():
            self._report("  ! No moves recommended: the PMQ1 mount calibration says the arrival order is swapped, "
                         "so the moves would steer the wrong magnets. Change 'First step to arrive', then Measure again.")
            return None
        rec = recommend(steps, self.calibrations["cage"], self.calibrations.get("mount"))
        mount_note = " (estimate)" if rec.mount_estimated else ""
        lines = [
            "  Recommended moves (+x/+y = the directions you bumped during calibration):",
            f"    1. Cage stage:  {_vec(rec.cage_um, 'µm', 1)}   (± {rec.cage_err_um[0]:.1f}, {rec.cage_err_um[1]:.1f})"
            f"   = {_vec(rec.cage_um / 1e3, 'mm', 4)}   -> nulls PMQ2's step",
            f"    2. PMQ1 mount{mount_note}: {_vec(rec.mount_um, 'µm', 1)}   (± {rec.mount_err_um[0]:.1f}, {rec.mount_err_um[1]:.1f})"
            f"   = {_vec(rec.mount_um / 1e3, 'mm', 4)}   -> then nulls PMQ1's step",
            f"    PMQ1 step after move 1 (predicted): {_vec(rec.S1_after_cage, 'mV')}",
        ]
        lines += [f"    ! {note}" for note in rec.notes]
        self._report("\n".join(lines))
        return rec
