"""Mini app for finding where a photodiode responds most steeply to position.

1. Pick the photodiode's channel, the step Δx, and put the diode at the scan origin (x = 0).
2. Collect: capture the channel and record its mean voltage at the current position. The next
   position advances by Δx; move the diode there and Collect again.
3. After three positions the app plots mean voltage and its slope against position and gives the
   offset from the origin where the slope is largest.

Talks to the scope only through the main app's local API (ScopeClient), so the main app must be
open and connected. Connecting to the scope directly would kick the app off.
"""

import time

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

from calibration.gradient import analyze, cubic_curve, measure_point
from calibration.session import Session
from desktop.main_window import CHANNEL_COLORS
from desktop.worker import Worker
from scope.client import ScopeClient
from scope.siglent import CHANNELS

UNITS = ("µm", "mm", "ticks")


def collect(client, channel):
    """Worker thread: one fresh capture of one channel -> ({channel: Trace}, trigger mode).

    Exactly one capture per Collect: back-to-back captures can catch the scope without a finished
    acquisition, and the read then times out and leaves it stopped.
    """
    status = client.status()
    if not status["connected"]:
        raise RuntimeError("The capture app is not connected to the scope: Connect it there, then try again")
    if channel not in status["enabled_channels"]:
        raise RuntimeError(f"{channel} is off on the scope: turn it on, then Collect again")
    return client.capture([channel]), status["trigger_mode"]


class CalibrationWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Photodiode Calibration")
        self.resize(1200, 820)

        self.client = ScopeClient()
        self.session = Session()
        self.settings = QSettings("researchMusumeci", "PhotodiodeCalibration")
        self._worker = None  # keep a reference so its signals outlive the pool's run()
        self.scan = 1
        self.points = []  # ScanPoint per Collect, in the order taken
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

        # Photodiode
        self.channel_box = QComboBox()
        self.channel_box.addItems(CHANNELS)
        self.channel_box.setCurrentText(self.settings.value("channel", "C2"))
        diode_form = QFormLayout()
        diode_form.addRow("Channel", self.channel_box)
        diode_group = QGroupBox("Photodiode")
        diode_group.setLayout(diode_form)

        # Scan
        self.unit_box = QComboBox()
        self.unit_box.setEditable(True)
        self.unit_box.addItems(UNITS)
        self.unit_box.setCurrentText(self.settings.value("unit", UNITS[0]))
        self.step_spin = QDoubleSpinBox()
        self.step_spin.setRange(-1e5, 1e5)
        self.step_spin.setDecimals(3)
        self.step_spin.setValue(float(self.settings.value("step", 25.0)))
        self.next_spin = QDoubleSpinBox()
        self.next_spin.setRange(-1e6, 1e6)
        self.next_spin.setDecimals(3)
        self.scan_label = QLabel()
        self.scan_label.setWordWrap(True)
        self.collect_btn = QPushButton("Collect")
        self.undo_btn = QPushButton("Undo last")
        self.new_btn = QPushButton("New scan")
        scan_form = QFormLayout()
        scan_form.addRow("Unit", self.unit_box)
        scan_form.addRow("Step Δx", self.step_spin)
        scan_form.addRow("Next position", self.next_spin)
        scan_buttons = QHBoxLayout()
        scan_buttons.addWidget(self.undo_btn)
        scan_buttons.addWidget(self.new_btn)
        scan_layout = QVBoxLayout()
        scan_layout.addLayout(scan_form)
        scan_layout.addWidget(self.scan_label)
        scan_layout.addWidget(self.collect_btn)
        scan_layout.addLayout(scan_buttons)
        scan_group = QGroupBox("Scan")
        scan_group.setLayout(scan_layout)

        # Result
        self.result_label = QLabel("Collect at least 3 positions.")
        self.result_label.setWordWrap(True)
        self.result_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        font = QFont(self.result_label.font())
        font.setPointSize(font.pointSize() + 2)
        self.result_label.setFont(font)
        result_layout = QVBoxLayout()
        result_layout.addWidget(self.result_label)
        result_group = QGroupBox("Steepest response")
        result_group.setLayout(result_layout)

        left_panel = QVBoxLayout()
        for group in (diode_group, scan_group, result_group):
            left_panel.addWidget(group)
        left_panel.addStretch()
        left_widget = QWidget()
        left_widget.setLayout(left_panel)
        left_widget.setFixedWidth(330)

        # Plots (response above slope, sharing x) above the results log
        self.plots = pg.GraphicsLayoutWidget()
        self.plots.setBackground("w")
        self.response_plot = self.plots.addPlot(row=0, col=0)
        self.slope_plot = self.plots.addPlot(row=1, col=0)
        self.slope_plot.setXLink(self.response_plot)
        for plot in (self.response_plot, self.slope_plot):
            plot.showGrid(x=True, y=True)
        self.results = QPlainTextEdit()
        self.results.setReadOnly(True)
        self.results.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))

        right = QSplitter(Qt.Vertical)
        right.addWidget(self.plots)
        right.addWidget(self.results)
        right.setSizes([540, 240])

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
        self.collect_btn.clicked.connect(self.on_collect_clicked)
        self.undo_btn.clicked.connect(self.on_undo_clicked)
        self.new_btn.clicked.connect(self.on_new_scan)
        self.channel_box.currentTextChanged.connect(lambda ch: (self.settings.setValue("channel", ch), self._redraw()))
        self.step_spin.valueChanged.connect(lambda step: self.settings.setValue("step", step))
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
        scanning = bool(self.points)
        self.refresh_btn.setEnabled(not busy)
        self.channel_box.setEnabled(not busy and not scanning)  # one scan is one diode
        for widget in (self.unit_box, self.step_spin, self.next_spin, self.collect_btn):
            widget.setEnabled(not busy)
        self.undo_btn.setEnabled(not busy and scanning)
        self.new_btn.setEnabled(not busy and scanning)
        self._update_scan_label()
        if message:
            self.statusBar().showMessage(message)

    def _unit(self):
        return self.unit_box.currentText().strip() or "units"

    def _x(self, x):
        return f"{x:+.4g} {self._unit()}"

    def _report(self, text):
        self.results.appendPlainText(text)
        self.results.verticalScrollBar().setValue(self.results.verticalScrollBar().maximum())

    def _update_scan_label(self):
        channel = self.channel_box.currentText()
        if not self.points:
            self.scan_label.setText(f"Scan {self.scan} on {channel}. Put the photodiode at the origin "
                                    "(Next position = 0), then Collect.")
            return
        self.scan_label.setText(f"Scan {self.scan} on {channel}: {len(self.points)} point(s), last at "
                                f"{self._x(self.points[-1].x)}. Move the photodiode to Next position, then Collect.")

    def on_failed(self, message):
        self._set_busy(False, message)
        self._report(f"! {message}")

    def on_unit_changed(self, unit):
        self.settings.setValue("unit", unit)
        for spin in (self.step_spin, self.next_spin):
            spin.setSuffix(f" {self._unit()}")
        self._redraw()

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
        # Offer enabled channels; keep the current choice if it is still offered.
        current = self.channel_box.currentText()
        self.channel_box.blockSignals(True)
        self.channel_box.clear()
        self.channel_box.addItems(enabled or CHANNELS)
        self.channel_box.setCurrentText(current)
        self.channel_box.blockSignals(False)
        self.settings.setValue("channel", self.channel_box.currentText())
        self._update_scan_label()

    # --- scan ---------------------------------------------------------------

    def on_collect_clicked(self):
        self._pending_x = self.next_spin.value()
        self._set_busy(True, f"Capturing {self.channel_box.currentText()} at {self._x(self._pending_x)}…")
        self._run(collect, self.on_collected, self.client, self.channel_box.currentText())

    def on_collected(self, result):
        traces, trigger_mode = result
        channel, x = self.channel_box.currentText(), self._pending_x
        point = measure_point(x, traces[channel].voltage)
        self.points.append(point)
        k = len(self.points)
        file = self.session.save_capture(self.scan, k, traces)
        self.session.save_scan(self.scan, channel, self._unit(), self.points)
        self.session.log({"kind": "point", "scan": self.scan, "channel": channel, "unit": self._unit(),
                          "point": point, "file": file, "trigger_mode": trigger_mode})

        self._report(f"[{time.strftime('%H:%M:%S')}] {channel} at x = {self._x(x)}: {point.mean_mv:.3f} ± "
                     f"{point.err_mv:.3f} mV (noise {point.noise_mv:.3f} mV)")
        self.next_spin.setValue(x + self.step_spin.value())
        self._redraw(report=True)
        self._set_busy(False, f"Point {k} collected. Move to {self._x(self.next_spin.value())} and Collect.")

    def on_undo_clicked(self):
        point = self.points.pop()
        self.next_spin.setValue(point.x)
        self.session.save_scan(self.scan, self.channel_box.currentText(), self._unit(), self.points)
        self.session.log({"kind": "undo", "scan": self.scan, "point": point})
        self._report(f"Removed the point at x = {self._x(point.x)}.")
        self._redraw(report=True)
        self._set_busy(False, "Last point removed")

    def on_new_scan(self):
        self.scan += 1
        self.points = []
        self.next_spin.setValue(0.0)
        self._report(f"\n--- Scan {self.scan} ---")
        self._redraw()
        self._set_busy(False, f"Scan {self.scan}: pick the channel and put the photodiode at the origin")

    # --- display ------------------------------------------------------------

    def _redraw(self, report=False):
        """Plot the scan and, from three positions on, the slope and where it peaks."""
        unit, channel = self._unit(), self.channel_box.currentText()
        color = CHANNEL_COLORS.get(channel, "#333333")
        self.response_plot.clear()
        self.slope_plot.clear()
        self.response_plot.setTitle(f"Scan {self.scan}: {channel} mean voltage")
        self.response_plot.setLabel("left", "mean (mV)")
        self.slope_plot.setTitle("Slope between neighboring points")
        self.slope_plot.setLabel("left", f"mV / {unit}")
        self.slope_plot.setLabel("bottom", f"position from origin ({unit})")
        if not self.points:
            self.result_label.setText("Collect at least 3 positions.")
            return

        try:
            result = analyze(self.points)
        except ValueError as e:
            result = None
            self.result_label.setText(str(e))
        if result is None:
            x = np.array([p.x for p in self.points])
            v = np.array([p.mean_mv for p in self.points])
            err = np.array([p.err_mv for p in self.points])
        else:
            x, v, err = result.x, result.v, result.err

        self.response_plot.addItem(pg.ErrorBarItem(x=x, y=v, height=2 * err, pen=pg.mkPen((120, 120, 120))))
        self.response_plot.plot(x, v, pen=pg.mkPen(color, width=1.5), symbol="o", symbolSize=7,
                                symbolBrush=color, symbolPen=None)
        if result is None:
            self.response_plot.autoRange()
            return

        self.slope_plot.addItem(pg.InfiniteLine(0, angle=0, pen=pg.mkPen((150, 150, 150))))
        self.slope_plot.plot(result.mid, result.slope, pen=pg.mkPen(color, width=1.5), symbol="s", symbolSize=6,
                             symbolBrush=color, symbolPen=None)
        for flank, name, style in ((result.best, "steepest", Qt.DashLine), (result.other, "other flank", Qt.DotLine)):
            if flank is None:
                continue
            curve = cubic_curve(result, flank)
            if curve is not None:
                self.response_plot.plot(*curve, pen=pg.mkPen((40, 40, 40), width=1.5, style=Qt.DashLine))
            for plot in (self.response_plot, self.slope_plot):
                plot.addItem(pg.InfiniteLine(flank.x, pen=pg.mkPen((40, 40, 40), style=style), label=name,
                                             labelOpts={"position": 0.9, "color": (40, 40, 40)}))
            self.slope_plot.plot([flank.x], [flank.slope], pen=None, symbol="star", symbolSize=14,
                                 symbolBrush=(40, 40, 40), symbolPen=None)
        self.response_plot.autoRange()
        self.slope_plot.autoRange()
        self._show_result(result, report)

    def _show_result(self, result, report):
        unit, best, last = self._unit(), result.best, self.points[-1].x
        text = (f"Move to x = {self._x(best.x)} from the origin\n"
                f"({best.x - last:+.4g} {unit} from the last point at {self._x(last)})\n"
                f"Slope there: {best.slope:+.4g} mV/{unit}")
        if result.other is not None:
            text += (f"\n\nOther flank (opposite sign): x = {self._x(result.other.x)}, "
                     f"slope {result.other.slope:+.4g} mV/{unit}")
        if result.notes:
            text += "\n\n" + "\n".join(f"! {note}" for note in result.notes)
        self.result_label.setText(text)
        if not report:
            return
        self._report("  " + text.replace("\n\n", "\n").replace("\n", "\n  "))
        self.session.log({"kind": "analysis", "scan": self.scan, "channel": self.channel_box.currentText(), "unit": unit,
                          "best": best, "other": result.other, "notes": result.notes})
