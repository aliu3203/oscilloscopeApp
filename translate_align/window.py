"""Mini app for aligning the PMQ doublet to the wire. Skeleton: pick the photodiode channels and capture.

Talks to the scope only through the main app's local API (ScopeClient), so the main app must be
open and connected. Connecting to the scope directly would kick the app off.
"""

import pyqtgraph as pg
from PySide6.QtCore import QSettings, QThreadPool
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from desktop.main_window import CHANNEL_COLORS
from desktop.worker import Worker
from scope.client import ScopeClient
from scope.siglent import CHANNELS

# Photodiode roles -> default channel. The diodes sense roughly x and y; which channel is which varies by setup.
DIODES = {"Diode X": "C2", "Diode Y": "C4"}


class AlignWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Translate Align")
        self.resize(1000, 650)

        self.client = ScopeClient()
        self.traces = {}  # role -> Trace from the last capture
        self._worker = None  # keep a reference so its signals outlive the pool's run()
        self.settings = QSettings("researchMusumeci", "TranslateAlign")

        # Connection row
        self.refresh_btn = QPushButton("Refresh")
        self.conn_label = QLabel("Not checked")

        conn_row = QHBoxLayout()
        conn_row.addWidget(QLabel(f"App API: {self.client.url}"))
        conn_row.addWidget(self.refresh_btn)
        conn_row.addWidget(self.conn_label)
        conn_row.addStretch()

        # Left panel: diode channel pickers + capture
        self.diode_boxes = {}
        diodes_layout = QFormLayout()
        for role, default in DIODES.items():
            box = QComboBox()
            box.addItems(CHANNELS)
            box.setCurrentText(self.settings.value(f"channel/{role}", default))
            box.currentTextChanged.connect(self.on_channel_changed)
            self.diode_boxes[role] = box
            diodes_layout.addRow(role, box)
        diodes_group = QGroupBox("Photodiodes")
        diodes_group.setLayout(diodes_layout)

        self.capture_btn = QPushButton("Capture")

        left_panel = QVBoxLayout()
        left_panel.addWidget(diodes_group)
        left_panel.addWidget(self.capture_btn)
        left_panel.addStretch()

        # Plot area
        self.plot = pg.PlotWidget(background="w")
        self.plot.showGrid(x=True, y=True)
        self.plot.setLabel("bottom", "Time", units="s")
        self.plot.setLabel("left", "Voltage", units="V")
        self.plot.setClipToView(True)
        self.plot.setDownsampling(auto=True, mode="peak")
        self.plot.addLegend()

        body = QHBoxLayout()
        body.addLayout(left_panel)
        body.addWidget(self.plot, stretch=1)

        root = QVBoxLayout()
        root.addLayout(conn_row)
        root.addLayout(body)

        central = QWidget()
        central.setLayout(root)
        self.setCentralWidget(central)

        self.statusBar().showMessage("Start the Oscilloscope Capture app and connect it, then Refresh")

        self.refresh_btn.clicked.connect(self.on_refresh_clicked)
        self.capture_btn.clicked.connect(self.on_capture_clicked)

    # --- helpers ------------------------------------------------------------

    def _run(self, fn, on_finished, on_failed, *args, **kwargs):
        self._worker = Worker(fn, *args, **kwargs)
        self._worker.signals.finished.connect(on_finished)
        self._worker.signals.failed.connect(on_failed)
        QThreadPool.globalInstance().start(self._worker)

    def _set_busy(self, busy):
        self.refresh_btn.setEnabled(not busy)
        self.capture_btn.setEnabled(not busy)
        for box in self.diode_boxes.values():
            box.setEnabled(not busy)  # on_captured reads the pickers to map channels to diodes

    def _diode_channels(self):
        return {role: box.currentText() for role, box in self.diode_boxes.items()}

    def on_failed(self, message):
        self._set_busy(False)
        self.statusBar().showMessage(message)

    def on_channel_changed(self, _):
        for role, channel in self._diode_channels().items():
            self.settings.setValue(f"channel/{role}", channel)

    # --- refresh ------------------------------------------------------------

    def on_refresh_clicked(self):
        self._set_busy(True)
        self.statusBar().showMessage("Checking app…")
        self._run(self.client.status, self.on_status, self.on_failed)

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

    # --- capture ------------------------------------------------------------

    def on_capture_clicked(self):
        diodes = self._diode_channels()
        channels = list(diodes.values())
        if len(set(channels)) != len(channels):
            self.statusBar().showMessage("Pick a different channel for each diode")
            return
        self._set_busy(True)
        self.statusBar().showMessage(f"Capturing {', '.join(channels)}…")
        self._run(self.client.capture, self.on_captured, self.on_failed, channels)

    def on_captured(self, traces):
        self._set_busy(False)
        self.traces = {role: traces[ch] for role, ch in self._diode_channels().items()}
        self.plot.clear()
        for role, trace in self.traces.items():
            pen = pg.mkPen(CHANNEL_COLORS[trace.channel], width=1)
            self.plot.plot(trace.time, trace.voltage, pen=pen, name=f"{role} ({trace.channel})")
        self.plot.autoRange()
        n_points = ", ".join(sorted({str(len(t)) for t in self.traces.values()}))
        self.statusBar().showMessage(f"Captured {', '.join(traces)} ({n_points} pts each)")
