"""Mini app for matching PMQ2's roll to PMQ1's: scan PMQ2's rotation, one A/B pair per reading.

For each PMQ2 reading:
1. Capture A with the wire at one off-axis point, move the cage stage, capture B.
2. The app subtracts the traces (B - A) point by point, finds both magnets' steps in the difference,
   and reads each one on both diodes: (dH1, dV1) for PMQ1 (fixed) and (dH2, dV2) for PMQ2 (in the
   rotation mount). The rolls match when dH1/dV1 = dH2/dV2.
3. Turn PMQ2 by Step Δ and repeat. The angle between the two change-vectors (less the target: opposite
   for a focusing-defocusing doublet) is plotted against the reading, and where the fitted line crosses
   zero is the reading to set.

Talks to the scope only through the main app's local API (ScopeClient), so the main app must be
open and connected. Connecting to the scope directly would kick the app off.
"""

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

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
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from desktop.main_window import CHANNEL_COLORS
from desktop.worker import Worker
from roll_align.compare import difference, fit_scan, measure_difference, refine_model
from scan_align.session import Session
from scan_align.zero import decimate
from scope.client import ScopeClient
from scope.siglent import CHANNELS
from translate_align.alignment import MAGNETS
from translate_align.steps import StepError, StepModel, locate_steps

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "roll_align"
SCAN_ALIGN_DIR = DATA_DIR.parent / "scan_align"  # its Locate records can seed the lock
# Photodiode roles -> default channel, horizontal first. Which channel is which varies by setup.
DIODES = {"Diode H": "C2", "Diode V": "C3"}
DEFAULT_FIRST = "PMQ2"  # its wave reaches the diodes first on this bench
FIXED, ADJUSTABLE = "PMQ1", "PMQ2"  # PMQ2 sits in the rotation mount
# Target angle from PMQ1's change-vector to PMQ2's. A focusing-defocusing doublet kicks the wire in
# opposite directions for the same move (write-up §7.3). The ratio test is matched at both.
TARGETS = {"opposite": ("Opposite (focusing–defocusing)", 180.0), "same": ("Same way", 0.0)}
ROLL_UNITS = ("deg", "turns", "ticks")
MAGNET_COLORS = {"PMQ1": (31, 119, 180), "PMQ2": (214, 39, 40)}


@dataclass
class Shot:
    """One capture of both diodes, and the PMQ2 reading and position label it was taken at."""

    raw: dict  # channel -> Trace as captured (saved to disk)
    traces: dict  # channel -> Trace, block-averaged for fitting
    reading: float
    where: str
    channels: dict  # role -> channel
    trigger_mode: str
    file: str = ""


@dataclass
class Point:
    """One PMQ2 reading of a rotation scan: its A/B pair and the comparison."""

    a: Shot
    b: Shot
    result: object  # RollResult
    fits: dict  # role -> StepFit on the difference trace

    @property
    def reading(self):
        return self.a.reading


def capture(client, channels):
    """Worker thread: exactly one capture of both diodes (back-to-back captures can stop the scope)."""
    status = client.status()
    if not status["connected"]:
        raise RuntimeError("The capture app is not connected to the scope: Connect it there, then try again")
    off = [ch for ch in channels.values() if ch not in status["enabled_channels"]]
    if off:
        raise RuntimeError(f"{', '.join(off)} off on the scope: turn it on, then try again")
    raw = client.capture(list(channels.values()))
    return raw, {ch: decimate(t) for ch, t in raw.items()}, status["trigger_mode"]


def earlier_locks():
    """Step locks recorded by earlier Roll Align and Scan Align sessions, newest first: (label, channels, models).

    Roles are matched by position (horizontal/x first), since Scan Align calls its diodes X and Y.
    """
    found = []
    for app, root in (("Roll Align", DATA_DIR), ("Scan Align", SCAN_ALIGN_DIR)):
        for log in root.glob("*/log.jsonl"):
            try:
                records = [json.loads(line) for line in open(log)]
            except (OSError, ValueError):
                continue
            for r in records:
                if r.get("kind") not in ("locate", "lock") or "models" not in r:
                    continue
                roles = list(r["models"])
                channels = {new: r["channels"][old] for new, old in zip(DIODES, roles)}
                models = {new: StepModel(**r["models"][old]) for new, old in zip(DIODES, roles)}
                times = ", ".join(f"{channels[k]} {m.t1:.0f}/{m.t2:.0f}" for k, m in models.items())
                found.append((r["time"], f"{r['time'].replace('T', ' ')}  {app}: {times} µs", channels, models))
    found.sort(key=lambda f: f[0], reverse=True)
    unique = {}
    for _, label, channels, models in found:
        unique.setdefault(label, (label, channels, models))
    return list(unique.values())


class RollWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Roll Align")
        self.resize(1250, 950)

        self.client = ScopeClient()
        self.session = Session(DATA_DIR)
        self.settings = QSettings("researchMusumeci", "RollAlign")
        self._worker = None  # keep a reference so its signals outlive the pool's run()
        self.models = None  # role -> StepModel, locked and kept across restarts
        self.channels = None  # role -> channel the models belong to
        self.lock_source = ""
        self.relocate = False  # locate on the next pair even though a lock exists
        self.scan = 1
        self.points = []  # Point per A/B pair of this scan (several per PMQ2 reading), in the order taken
        self.shots = {"A": None, "B": None}  # the current pair
        self.pair_point = None  # index in points of the current pair, once B is captured (B again replaces it)
        self.n_captures = 0
        self._pending = None

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

        # Step lock
        self.lock_label = QLabel()
        self.lock_label.setWordWrap(True)
        self.relocate_btn = QPushButton("Locate on the next pair")
        self.earlier_btn = QPushButton("Use an earlier lock…")
        lock_buttons = QHBoxLayout()
        lock_buttons.addWidget(self.relocate_btn)
        lock_buttons.addWidget(self.earlier_btn)
        lock_layout = QVBoxLayout()
        lock_layout.addWidget(self.lock_label)
        lock_layout.addLayout(lock_buttons)
        lock_group = QGroupBox("1. Step times")
        lock_group.setLayout(lock_layout)

        # Rotation scan
        self.target_box = QComboBox()
        for key, (name, _) in TARGETS.items():
            self.target_box.addItem(name, key)
        self.target_box.setCurrentIndex(max(0, self.target_box.findData(self.settings.value("target", "opposite"))))
        self.roll_unit_box = QComboBox()
        self.roll_unit_box.setEditable(True)
        self.roll_unit_box.addItems(ROLL_UNITS)
        self.roll_unit_box.setCurrentText(self.settings.value("roll_unit", ROLL_UNITS[0]))
        self.step_spin = QDoubleSpinBox()
        self.step_spin.setRange(-1e5, 1e5)
        self.step_spin.setDecimals(3)
        self.step_spin.setValue(float(self.settings.value("step", 2.0)))
        self.repeats_spin = QSpinBox()
        self.repeats_spin.setRange(1, 20)
        self.repeats_spin.setValue(int(self.settings.value("repeats", 3)))
        self.reading_spin = QDoubleSpinBox()
        self.reading_spin.setRange(-1e6, 1e6)
        self.reading_spin.setDecimals(3)
        self.reading_spin.setValue(float(self.settings.value("reading", 0.0)))
        scan_form = QFormLayout()
        scan_form.addRow("Target", self.target_box)
        scan_form.addRow("Unit", self.roll_unit_box)
        scan_form.addRow("Step Δ", self.step_spin)
        scan_form.addRow("Pairs per reading", self.repeats_spin)
        scan_form.addRow("PMQ2 reading", self.reading_spin)
        self.where_edits, self.capture_btns = {}, {}
        ab_rows = QVBoxLayout()
        for key in self.shots:
            edit = QLineEdit(self.settings.value(f"where/{key}", ""))
            edit.setPlaceholderText("cage reading (optional)")
            edit.textChanged.connect(lambda text, k=key: self.settings.setValue(f"where/{k}", text))
            button = QPushButton(f"Capture {key}")
            button.clicked.connect(lambda _=False, k=key: self.on_capture_clicked(k))
            row = QHBoxLayout()
            row.addWidget(button)
            row.addWidget(edit, stretch=1)
            ab_rows.addLayout(row)
            self.where_edits[key], self.capture_btns[key] = edit, button
        self.scan_label = QLabel()
        self.scan_label.setWordWrap(True)
        self.undo_btn = QPushButton("Undo last")
        self.new_btn = QPushButton("New scan")
        scan_buttons = QHBoxLayout()
        scan_buttons.addWidget(self.undo_btn)
        scan_buttons.addWidget(self.new_btn)
        scan_layout = QVBoxLayout()
        scan_layout.addLayout(scan_form)
        scan_layout.addLayout(ab_rows)
        scan_layout.addWidget(self.scan_label)
        scan_layout.addLayout(scan_buttons)
        scan_group = QGroupBox("2. Rotation scan")
        scan_group.setLayout(scan_layout)

        # Result
        self.result_label = QLabel()
        self.result_label.setWordWrap(True)
        self.result_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        font = QFont(self.result_label.font())
        font.setPointSize(font.pointSize() + 1)
        self.result_label.setFont(font)
        self.result_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        result_scroll = QScrollArea()
        result_scroll.setWidgetResizable(True)
        result_scroll.setFrameShape(QScrollArea.NoFrame)
        result_scroll.setWidget(self.result_label)
        result_layout = QVBoxLayout()
        result_layout.addWidget(result_scroll)
        result_group = QGroupBox("Result")
        result_group.setLayout(result_layout)

        left_panel = QVBoxLayout()
        for group in (diodes_group, lock_group, scan_group):
            left_panel.addWidget(group)
        left_panel.addWidget(result_group, stretch=1)
        left_widget = QWidget()
        left_widget.setLayout(left_panel)
        left_widget.setFixedWidth(370)

        # Plots: angle vs. reading on top; the last pair's change-vectors and difference trace below
        self.plots = pg.GraphicsLayoutWidget()
        self.plots.setBackground("w")
        self.scan_plot = self.plots.addPlot(row=0, col=0, colspan=2)
        self.scan_plot.showGrid(x=True, y=True)
        self.scan_plot.getAxis("left").enableAutoSIPrefix(False)
        self.vector_plot = self.plots.addPlot(row=1, col=0)
        self.vector_plot.showGrid(x=True, y=True)
        self.vector_plot.setAspectLocked(True)
        self.vector_plot.setLabel("bottom", "dH (mV)")
        self.vector_plot.setLabel("left", "dV (mV)")
        self.vector_plot.setTitle("Last pair: change of each step, A → B")
        self.vector_plot.addLegend(offset=(10, 10))
        for axis in ("left", "bottom"):
            self.vector_plot.getAxis(axis).enableAutoSIPrefix(False)
        self.diff_plot = self.plots.addPlot(row=1, col=1)
        self.diff_plot.showGrid(x=True, y=True)
        self.diff_plot.setLabel("left", "B − A (mV)")
        self.diff_plot.setLabel("bottom", "time after trigger (µs)")
        self.diff_plot.setTitle("Last pair: B − A, tone removed, and the step fit")
        self.diff_plot.addLegend(offset=(10, 10))
        self.diff_plot.getAxis("left").enableAutoSIPrefix(False)
        self.plots.ci.layout.setColumnStretchFactor(0, 2)
        self.plots.ci.layout.setColumnStretchFactor(1, 3)
        self.results = QPlainTextEdit()
        self.results.setReadOnly(True)
        self.results.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))

        right = QSplitter(Qt.Vertical)
        right.addWidget(self.plots)
        right.addWidget(self.results)
        right.setSizes([700, 220])

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
        self.relocate_btn.clicked.connect(self.on_relocate_clicked)
        self.earlier_btn.clicked.connect(self.on_earlier_clicked)
        self.undo_btn.clicked.connect(self.on_undo_clicked)
        self.new_btn.clicked.connect(self.on_new_scan)
        self.target_box.currentIndexChanged.connect(self.on_target_changed)
        self.roll_unit_box.currentTextChanged.connect(self.on_roll_unit_changed)
        self.step_spin.valueChanged.connect(lambda step: self.settings.setValue("step", step))
        self.repeats_spin.valueChanged.connect(self.on_repeats_changed)
        self.reading_spin.valueChanged.connect(self.on_reading_changed)

        self._load_lock()
        self.on_roll_unit_changed(self.roll_unit_box.currentText())
        self._set_busy(False)
        self.statusBar().showMessage("Start the Oscilloscope Capture app and connect it, then Refresh")

    # --- helpers ------------------------------------------------------------

    def _run(self, fn, on_finished, *args, **kwargs):
        self._worker = Worker(fn, *args, **kwargs)
        self._worker.signals.finished.connect(on_finished)
        self._worker.signals.failed.connect(self.on_failed)
        QThreadPool.globalInstance().start(self._worker)

    def _set_busy(self, busy, message=None):
        for widget in (self.refresh_btn, self.capture_btns["A"], *self.diode_boxes.values(), self.first_box,
                       self.relocate_btn, self.earlier_btn, self.new_btn):
            widget.setEnabled(not busy)
        self.capture_btns["B"].setEnabled(not busy and self.shots["A"] is not None)
        self.undo_btn.setEnabled(not busy and bool(self.points))
        self._update_scan_label()
        if message:
            self.statusBar().showMessage(message)

    def _order(self):
        """Magnets in the order their steps arrive at the diodes."""
        first = self.first_box.currentText()
        return (first, *(m for m in MAGNETS if m != first))

    def _diode_channels(self):
        return {role: box.currentText() for role, box in self.diode_boxes.items()}

    def _target(self):
        return TARGETS[self.target_box.currentData()][1]

    def _target_name(self):
        return TARGETS[self.target_box.currentData()][0].lower()

    def _unit(self):
        return self.roll_unit_box.currentText().strip() or "units"

    def _reading(self, value):
        return f"{value:.5g} {self._unit()}"

    def _report(self, text):
        self.results.appendPlainText(text)
        self.results.verticalScrollBar().setValue(self.results.verticalScrollBar().maximum())

    def on_failed(self, message):
        self._set_busy(False, message)
        self._report(f"! {message}")

    def _update_scan_label(self):
        a, b = self.shots["A"], self.shots["B"]
        groups = len({p.reading for p in self.points})
        what = f"Scan {self.scan}: {len(self.points)} pair(s) at {groups} reading(s). "
        if a is None:
            reading = self.reading_spin.value()
            done = self._pairs_at(reading)
            if done:
                text = (f"Pair {done + 1} of {self.repeats_spin.value()} at PMQ2 {self._reading(reading)}: leave PMQ2 there "
                        "and Capture A where the cage is, move it, Capture B.")
            else:
                text = (f"Turn PMQ2 to {self._reading(reading)} (or set PMQ2 reading to where it is), then Capture A. "
                        "The cage can start where it is: A and B may swap between pairs.")
        elif b is None:
            text = f"A taken at PMQ2 {self._reading(a.reading)}. Move the cage stage, then Capture B."
        elif self.reading_spin.value() == a.reading:
            text = (f"Pair {self._pairs_at(a.reading)} of {self.repeats_spin.value()} done at PMQ2 {self._reading(a.reading)}. "
                    "Leave PMQ2 there and Capture A for the next pair. (Capture B again replaces this pair's B.)")
        else:
            text = (f"Done at PMQ2 {self._reading(a.reading)}. Turn PMQ2 to {self._reading(self.reading_spin.value())}, "
                    "then Capture A. (Capture B again replaces this pair's B.)")
        self.scan_label.setText(what + text)

    # --- settings -----------------------------------------------------------

    def on_reading_changed(self, value):
        self.settings.setValue("reading", value)
        self._update_scan_label()

    def on_repeats_changed(self, n):
        self.settings.setValue("repeats", n)
        self._update_scan_label()

    def _pairs_at(self, reading):
        return sum(1 for p in self.points if p.reading == reading)

    def on_roll_unit_changed(self, unit):
        self.settings.setValue("roll_unit", unit)
        for spin in (self.step_spin, self.reading_spin):
            spin.setSuffix(f" {self._unit()}")
        self._redraw()
        self._update_scan_label()

    def on_target_changed(self, _):
        self.settings.setValue("target", self.target_box.currentData())
        if self.points:
            self._save_scan()
        self._redraw(report=bool(self.points))

    def on_channel_changed(self, _):
        channels = self._diode_channels()
        for role, channel in channels.items():
            self.settings.setValue(f"channel/{role}", channel)
        if self.shots["A"] is not None and channels != self.shots["A"].channels:
            self.shots, self.pair_point = {"A": None, "B": None}, None
            self._report("Diode channels changed: the current pair was cleared.")
        self._show_lock()
        self._set_busy(False)

    def on_order_changed(self, first):
        self.settings.setValue("first_to_arrive", first)
        self._report(f"First step to arrive is now {first}.")
        self._show_lock()
        self._remeasure()

    # --- step lock ----------------------------------------------------------

    def _load_lock(self):
        try:
            lock = json.loads(self.settings.value("lock", ""))
            self.channels = lock["channels"]
            self.models = {role: StepModel(**m) for role, m in lock["models"].items()}
            self.lock_source = lock.get("source", "")
        except (ValueError, TypeError, KeyError):
            self.channels, self.models, self.lock_source = None, None, ""
        self._show_lock()

    def _set_lock(self, channels, models, source):
        self.channels, self.models, self.lock_source, self.relocate = dict(channels), models, source, False
        self.settings.setValue("lock", json.dumps({"channels": self.channels, "source": source,
                                                   "models": {r: asdict(m) for r, m in models.items()}}))
        self.session.log({"kind": "lock", "source": source, "channels": self.channels, "models": models})
        first, second = self._order()
        self._report(f"Step times locked ({source}): " + "; ".join(
            f"{channels[r]} {first} {m.t1:.0f} µs (edge {m.w1:.0f}), {second} {m.t2:.0f} µs (edge {m.w2:.0f})"
            for r, m in models.items()))
        self._show_lock()

    def _lock_usable(self):
        return self.models is not None and not self.relocate and self.channels == self._diode_channels()

    def _show_lock(self):
        if self.models is None:
            text = "No lock yet: the steps are located on the first pair's difference."
        elif self.channels != self._diode_channels():
            text = "The saved lock is for other channels: the steps are located on the next pair."
        else:
            first, second = self._order()
            text = (f"Locked ({self.lock_source}): " + "; ".join(
                f"{self.channels[r]} {first} {m.t1:.0f}, {second} {m.t2:.0f} µs" for r, m in self.models.items()))
            if self.relocate:
                text += ". Locating again on the next pair."
        self.lock_label.setText(text + " The dotted lines should sit on the edges of the difference trace.")

    def on_relocate_clicked(self):
        self.relocate = True
        self._show_lock()
        self._report("The steps will be located again on the next pair.")

    def on_earlier_clicked(self):
        locks = earlier_locks()
        if not locks:
            self.statusBar().showMessage("No earlier locks found")
            return
        label, ok = QInputDialog.getItem(self, "Use an earlier lock", "Step times from:", [lk[0] for lk in locks], 0, False)
        if not ok:
            return
        _, channels, models = next(lk for lk in locks if lk[0] == label)
        for role, box in self.diode_boxes.items():
            box.blockSignals(True)
            box.setCurrentText(channels[role])
            box.blockSignals(False)
        self._set_lock(channels, models, f"from {label.split('  ')[0]}")
        self.on_channel_changed(None)
        self._remeasure()

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
        for box in self.diode_boxes.values():
            current = box.currentText()
            box.blockSignals(True)
            box.clear()
            box.addItems(enabled or CHANNELS)
            box.setCurrentText(current)
            box.blockSignals(False)
        self.on_channel_changed(None)

    # --- capture ------------------------------------------------------------

    def on_capture_clicked(self, key):
        channels = self._diode_channels()
        if len(set(channels.values())) != len(channels):
            self.statusBar().showMessage("Pick a different channel for each diode")
            return
        # B belongs to A's pair, so it keeps A's reading even after the spin box has moved on
        reading = self.reading_spin.value() if key == "A" else self.shots["A"].reading
        self._pending = (key, channels, reading, self.where_edits[key].text().strip())
        self._set_busy(True, f"Capturing {key}…")
        self._run(capture, self.on_captured, self.client, channels)

    def on_captured(self, result):
        raw, traces, trigger_mode = result
        key, channels, reading, where = self._pending
        self.n_captures += 1
        shot = Shot(raw, traces, reading, where, channels, trigger_mode)
        shot.file = self.session.save_capture(f"cap{self.n_captures:03d}_{key}", raw)
        self.session.log({"kind": "capture", "scan": self.scan, "point": key, "where": where, "pmq2_reading": reading,
                          "unit": self._unit(), "channels": channels, "file": shot.file, "trigger_mode": trigger_mode})
        self._report(f"[{time.strftime('%H:%M:%S')}] Captured {key}{f' at {where}' if where else ''} "
                     f"(PMQ2 {self._reading(reading)})")
        if trigger_mode == "AUTO":
            self._report("  ! Scope is in AUTO trigger, so a capture can be one without a pulse. Use NORMAL trigger on the pulse.")
        if key == "A":
            self.shots, self.pair_point = {"A": shot, "B": None}, None
            self._set_busy(False, "A captured. Move the cage stage and capture B.")
            return
        self.shots["B"] = shot
        if self._compare_pair() and self._pairs_at(reading) >= self.repeats_spin.value():
            self.reading_spin.setValue(reading + self.step_spin.value())
        self._set_busy(False, "B captured.")

    def _measure(self, a, b):
        """(fits, RollResult) of one pair with the locked step times."""
        diff = difference(a.traces, b.traces)
        return measure_difference(diff, a.channels, self.models, self._order(), FIXED, ADJUSTABLE)

    def _compare_pair(self):
        a, b = self.shots["A"], self.shots["B"]
        try:
            relocked = not self._lock_usable()
            if relocked:
                diff = difference(a.traces, b.traces)
                models = {role: refine_model(diff[ch], locate_steps(diff[ch])) for role, ch in a.channels.items()}
                self._set_lock(a.channels, models, f"located on {a.file} → {b.file}")
            fits, result = self._measure(a, b)
        except (ValueError, StepError) as e:
            self._report(f"! {e}")
            self.statusBar().showMessage(str(e))
            return False
        point = Point(a, b, result, fits)
        if self.pair_point is None:
            self.points.append(point)
            self.pair_point = len(self.points) - 1
        else:
            self.points[self.pair_point] = point  # B captured again
        if relocked and len(self.points) > 1:
            self._remeasure(redraw=False)
        self.session.log({"kind": "pair", "scan": self.scan, "point": self.pair_point + 1, "files": [a.file, b.file],
                          "where": [a.where, b.where], "pmq2_reading": point.reading, "unit": self._unit(),
                          "channels": a.channels, "target": self._target(),
                          "off_target": result.off_target(self._target()), "result": result,
                          "fit_noise_uv": {r: f.resid_uv for r, f in fits.items()}})
        self._save_scan()
        self._report(self._pair_text(point))
        self._redraw(report=True)
        return True

    def _remeasure(self, redraw=True):
        """Measure every pair of this scan again with the current lock and arrival order."""
        if not self.points or not self._lock_usable():
            return
        try:
            points = []
            for p in self.points:
                fits, result = self._measure(p.a, p.b)
                points.append(Point(p.a, p.b, result, fits))
            self.points = points
        except (ValueError, StepError) as e:
            self._report(f"! {e}")
            return
        self._report("  The pairs of this scan were measured again with the current step times.")
        self._save_scan()
        if redraw:
            self._redraw(report=True)

    # --- scan ---------------------------------------------------------------

    def on_undo_clicked(self):
        point = self.points.pop()
        self.shots, self.pair_point = {"A": None, "B": None}, None
        self.reading_spin.setValue(point.reading)
        self.session.log({"kind": "undo", "scan": self.scan, "pmq2_reading": point.reading})
        self._save_scan()
        self._report(f"Removed the pair at PMQ2 {self._reading(point.reading)}.")
        self._redraw(report=True)
        self._set_busy(False, "Last pair removed")

    def on_new_scan(self):
        self.scan += 1
        self.points, self.shots, self.pair_point = [], {"A": None, "B": None}, None
        self._report(f"\n--- Scan {self.scan} ---")
        self._redraw()
        self._set_busy(False, f"Scan {self.scan}: set the PMQ2 reading and Capture A")

    def _save_scan(self):
        self.session.dir.mkdir(parents=True, exist_ok=True)
        with open(self.session.dir / f"scan{self.scan:02d}_roll.csv", "w") as f:
            f.write(f"reading_{self._unit()},off_target_deg,err_deg,line_angle_deg,turn_deg,"
                    "PMQ1_dH_mV,PMQ1_dV_mV,PMQ2_dH_mV,PMQ2_dV_mV,PMQ1_dH_over_dV,PMQ2_dH_over_dV,file_A,file_B\n")
            for p in self.points:
                r = p.result
                f.write(",".join([f"{p.reading:.6g}", f"{r.off_target(self._target()):.4f}", f"{r.mismatch_err:.4f}",
                                  f"{r.mismatch:.4f}", f"{r.turn:.4f}",
                                  *(f"{v:.5f}" for m in (FIXED, ADJUSTABLE) for v in r.dS[m]),
                                  *(f"{r.ratio[m][0]:.5f}" for m in (FIXED, ADJUSTABLE)), p.a.file, p.b.file]) + "\n")

        scan = self._scan_fit()
        with open(self.session.dir / f"scan{self.scan:02d}_roll_readings.csv", "w") as f:
            f.write(f"reading_{self._unit()},mean_off_target_deg,err_deg,n_pairs,sd_deg\n")
            for g in scan.groups:
                f.write(f"{g.reading:.6g},{g.mean:.4f},{g.err:.4f},{g.n},{g.sd:.4f}\n")

    # --- text ---------------------------------------------------------------

    def _magnet_line(self, result, magnet):
        (dH, dV), (eH, eV) = result.dS[magnet], result.err[magnet]
        r, er = result.ratio[magnet]
        return f"dH {dH:+.3f}±{eH:.3f}, dV {dV:+.3f}±{eV:.3f} mV, dH/dV {r:+.3f}±{er:.3f}"

    def _pair_lines(self, point):
        r = point.result
        return [f"PMQ1 (fixed): {self._magnet_line(r, FIXED)}",
                f"PMQ2 (rotating): {self._magnet_line(r, ADJUSTABLE)}",
                f"Lines differ by {r.mismatch:+.2f} ± {r.mismatch_err:.2f}° (0 when the ratios match).",
                f"PMQ2's change is turned {r.turn:+.1f}° from PMQ1's: {r.off_target(self._target()):+.1f}° "
                f"from {self._target_name()}.",
                *r.notes]

    def _pair_text(self, point):
        where = " → ".join(s.where or k for s, k in ((point.a, "A"), (point.b, "B")))
        head = (f"[{time.strftime('%H:%M:%S')}] Scan {self.scan} pair {self.pair_point + 1} at PMQ2 "
                f"{self._reading(point.reading)} ({where})")
        return "\n".join([head, *(f"  {line}" for line in self._pair_lines(point))])

    def _scan_fit(self):
        return fit_scan([p.reading for p in self.points], [p.result for p in self.points], self._target())

    def _scan_lines(self, scan):
        if scan.fit is None:
            return ["Turn PMQ2 by Step Δ and take another pair: the zero needs two readings."]
        lines = []
        if np.isfinite(scan.zero):
            lines.append(f"<b>Set PMQ2 to {self._reading(scan.zero)} ± {scan.zero_err:.2g}</b> "
                         f"({self._target_name()}, from {len(self.points)} pairs at {len(scan.groups)} readings)")
        lines.append(f"Slope {scan.fit.slope:+.3g}° per {self._unit()}: turning PMQ2 by 1° turns its change by about 2°.")
        return lines + [f"<i>{note}</i>" for note in scan.notes]

    def _group_lines(self, scan):
        lines = [f"PMQ2 {self._reading(g.reading)}: {g.mean:+.2f} ± {g.err:.2f}° off target"
                 + (f" (mean of {g.n} pairs, SD {g.sd:.2f}°)" if g.n > 1 else " (1 pair)") for g in scan.groups]
        if np.isfinite(scan.pooled_sd):
            lines.append(f"One pair scatters by about {scan.pooled_sd:.2f}° (SD pooled over the repeats).")
        return lines

    # --- display ------------------------------------------------------------

    def _redraw(self, report=False):
        plot = self.scan_plot
        plot.clear()
        plot.setTitle(f"Scan {self.scan}: angle of PMQ2's change from {self._target_name()} to PMQ1's, vs. PMQ2 reading")
        plot.setLabel("left", "off target (°)")
        plot.setLabel("bottom", f"PMQ2 reading ({self._unit()})")
        if not self.points:
            plot.addItem(pg.InfiniteLine(0, angle=0, pen=pg.mkPen((90, 90, 90), width=1.5)))
            self.result_label.setText("Set the PMQ2 reading, then Capture A, move the cage, Capture B.")
            self.vector_plot.clear()
            self.diff_plot.clear()
            return
        scan = self._scan_fit()
        color = MAGNET_COLORS[ADJUSTABLE]
        gx = np.array([g.reading for g in scan.groups])
        gy = np.array([g.mean for g in scan.groups])
        ge = np.array([g.err for g in scan.groups])
        target_level = 0.0
        if scan.fit is not None and np.isfinite(scan.zero):
            target_level = scan.fit.slope * (scan.zero - scan.fit.x0)  # the multiple of 360° the line reaches at the zero
            lo, hi = min(gx.min(), scan.zero), max(gx.max(), scan.zero)
            xs = np.array([lo, hi])
            plot.plot(xs, scan.fit.slope * (xs - scan.fit.x0), pen=pg.mkPen(color, width=2))
            plot.addItem(pg.InfiniteLine(scan.zero, pen=pg.mkPen((20, 20, 20), width=1.5, style=Qt.DashLine),
                                         label=f"set PMQ2 here: {scan.zero:.4g}",
                                         labelOpts={"position": 0.08, "color": (20, 20, 20)}))
        plot.addItem(pg.InfiniteLine(target_level, angle=0, pen=pg.mkPen((90, 90, 90), width=1.5)))
        # each pair as a small hollow dot; each reading's mean with its standard error
        plot.plot(scan.readings, scan.angles, pen=None, symbol="o", symbolSize=6, symbolBrush=None,
                  symbolPen=pg.mkPen((*color, 140), width=1))
        plot.addItem(pg.ErrorBarItem(x=gx, y=gy, height=2 * ge, beam=0.12 * self._beam(gx), pen=pg.mkPen(color, width=1.5)))
        plot.plot(gx, gy, pen=None, symbol="o", symbolSize=10, symbolBrush=color, symbolPen=None)
        plot.autoRange()

        last = self.points[-1]
        lines = (self._scan_lines(scan) + ["", "<b>Readings</b>", *self._group_lines(scan)]
                 + ["", f"<b>Last pair</b> (PMQ2 {self._reading(last.reading)}):", *self._pair_lines(last)])
        self.result_label.setText("<br>".join(lines))
        if report and scan.fit is not None:
            self._report("\n".join("  " + line.replace("<b>", "").replace("</b>", "").replace("<i>", "! ").replace("</i>", "")
                                   for line in self._scan_lines(scan)))
        self._draw_vectors(last.result)
        self._draw_difference(last.fits, last.a.channels)

    @staticmethod
    def _beam(x):
        """Error bar cap width: a fraction of the smallest spacing between readings."""
        gaps = np.diff(np.unique(x))
        return float(gaps.min()) if gaps.size else 1.0

    def _draw_difference(self, fits, channels):
        plot = self.diff_plot
        plot.clear()
        for role, fit in fits.items():
            channel, model = channels[role], self.models[role]
            plot.plot(fit.t, fit.clean, pen=pg.mkPen((180, 180, 180), width=1))
            plot.plot(fit.t, fit.model, pen=pg.mkPen(CHANNEL_COLORS.get(channel, "#333333"), width=2.5),
                      name=f"{role} ({channel})")
            for t0, magnet in zip((model.t1, model.t2), self._order()):
                plot.addItem(pg.InfiniteLine(t0, pen=pg.mkPen(MAGNET_COLORS[magnet], style=Qt.DotLine), label=magnet,
                                             labelOpts={"position": 0.9, "color": MAGNET_COLORS[magnet]}))
        plot.autoRange()

    def _draw_vectors(self, result):
        plot = self.vector_plot
        plot.clear()
        reach = max(np.linalg.norm(v) for v in result.dS.values()) * 1.15
        u = result.dS[FIXED] / np.linalg.norm(result.dS[FIXED])
        plot.plot([-reach * u[0], reach * u[0]], [-reach * u[1], reach * u[1]],
                  pen=pg.mkPen(MAGNET_COLORS[FIXED], width=1, style=Qt.DashLine), name="PMQ1's line")
        for magnet in (FIXED, ADJUSTABLE):
            (dH, dV), (eH, eV) = result.dS[magnet], result.err[magnet]
            color = MAGNET_COLORS[magnet]
            plot.plot([0, dH], [0, dV], pen=pg.mkPen(color, width=3), name=magnet)
            plot.plot([dH], [dV], pen=None, symbol="o", symbolSize=9, symbolBrush=color, symbolPen=None)
            plot.addItem(pg.ErrorBarItem(x=np.array([dH]), y=np.array([dV]), height=np.array([2 * eV]),
                                         width=np.array([2 * eH]), pen=pg.mkPen(color)))
        plot.addItem(pg.ScatterPlotItem([0], [0], size=6, brush=(60, 60, 60)))
        plot.setXRange(-reach, reach)
        plot.setYRange(-reach, reach)
