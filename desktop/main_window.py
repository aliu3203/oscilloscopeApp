import pyqtgraph as pg
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import (
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from desktop.worker import Worker
from scope.siglent import CHANNELS, SiglentScope

DEFAULT_IP = "10.0.0.237"

# Roughly the scope's own channel colors, darkened to be readable on a white background.
CHANNEL_COLORS = {"C1": "#b8960b", "C2": "#d6249f", "C3": "#008b9a", "C4": "#1f4fd1"}


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Oscilloscope Capture")
        self.resize(1100, 700)

        self.scope = None
        self.traces = {}
        self._worker = None  # keep a reference so its signals outlive the pool's run()

        # Connection row
        self.ip_edit = QLineEdit(DEFAULT_IP)
        self.ip_edit.setFixedWidth(160)
        self.connect_btn = QPushButton("Connect")
        self.conn_label = QLabel("Not connected")

        conn_row = QHBoxLayout()
        conn_row.addWidget(QLabel("Scope IP:"))
        conn_row.addWidget(self.ip_edit)
        conn_row.addWidget(self.connect_btn)
        conn_row.addWidget(self.conn_label)
        conn_row.addStretch()

        # Left panel: channels + actions
        self.channel_boxes = {ch: QCheckBox(f"CH{ch[1:]}") for ch in CHANNELS}
        self.capture_btn = QPushButton("Capture")
        self.save_btn = QPushButton("Save…")
        self.capture_btn.setEnabled(False)
        self.save_btn.setEnabled(False)

        channels_layout = QVBoxLayout()
        for box in self.channel_boxes.values():
            channels_layout.addWidget(box)
        channels_group = QGroupBox("Channels")
        channels_group.setLayout(channels_layout)

        left_panel = QVBoxLayout()
        left_panel.addWidget(channels_group)
        left_panel.addWidget(self.capture_btn)
        left_panel.addWidget(self.save_btn)
        left_panel.addStretch()

        # Plot area
        self.plot = pg.PlotWidget(background="w")
        self.plot.showGrid(x=True, y=True)
        self.plot.setLabel("bottom", "Time", units="s")
        self.plot.setLabel("left", "Voltage", units="V")
        self.plot.setClipToView(True)
        self.plot.setDownsampling(auto=True, mode="peak")  # keeps 14M-point captures responsive
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

        self.statusBar().showMessage("Ready")

        self.connect_btn.clicked.connect(self.on_connect_clicked)
        self.capture_btn.clicked.connect(self.on_capture_clicked)

    # --- helpers ------------------------------------------------------------

    def _run(self, fn, on_finished, on_failed, *args, **kwargs):
        self._worker = Worker(fn, *args, **kwargs)
        self._worker.signals.finished.connect(on_finished)
        self._worker.signals.failed.connect(on_failed)
        QThreadPool.globalInstance().start(self._worker)

    def _set_busy(self, busy):
        connected = self.scope is not None and self.scope.is_connected
        self.connect_btn.setEnabled(not busy)
        self.ip_edit.setEnabled(not busy and not connected)
        self.capture_btn.setEnabled(not busy and connected)

    def _set_disconnected(self, message):
        if self.scope is not None:
            self.scope.close()
        self.scope = None
        self.connect_btn.setText("Connect")
        self.conn_label.setText("Not connected")
        self._set_busy(False)
        self.statusBar().showMessage(message)

    # --- connect ------------------------------------------------------------

    def on_connect_clicked(self):
        if self.scope is not None and self.scope.is_connected:
            self._set_disconnected("Disconnected")
            return

        self.scope = SiglentScope(self.ip_edit.text().strip())
        self._set_busy(True)
        self.statusBar().showMessage(f"Connecting to {self.scope.host}…")
        self._run(self._connect_and_list_channels, self.on_connected, self.on_connect_failed)

    def _connect_and_list_channels(self):
        # Runs on a worker thread.
        self.scope.connect()
        return self.scope.enabled_channels()

    def on_connected(self, enabled):
        for ch, box in self.channel_boxes.items():
            box.setChecked(ch in enabled)
        self.connect_btn.setText("Disconnect")
        self.conn_label.setText(f"Connected: {self.scope.model}")
        self._set_busy(False)
        self.statusBar().showMessage(f"Connected to {self.scope.idn}")

    def on_connect_failed(self, message):
        self._set_disconnected(message)

    # --- capture ------------------------------------------------------------

    def on_capture_clicked(self):
        channels = [ch for ch, box in self.channel_boxes.items() if box.isChecked()]
        if not channels:
            self.statusBar().showMessage("Select at least one channel")
            return
        self._set_busy(True)
        self.statusBar().showMessage(f"Capturing {', '.join(channels)}…")
        self._run(self.scope.get_traces, self.on_captured, self.on_capture_failed, channels, freeze=True)

    def on_captured(self, traces):
        self.traces = traces
        self.plot.clear()
        for ch, trace in traces.items():
            self.plot.plot(trace.time, trace.voltage, pen=pg.mkPen(CHANNEL_COLORS[ch], width=1), name=ch)
        self.plot.autoRange()
        self._set_busy(False)
        n_points = ", ".join(sorted({str(len(t)) for t in traces.values()}))
        self.statusBar().showMessage(f"Captured {', '.join(traces)} ({n_points} pts each)")

    def on_capture_failed(self, message):
        if self.scope is not None and not self.scope.is_connected:
            self._set_disconnected(message)
        else:
            self._set_busy(False)
            self.statusBar().showMessage(message)

    def closeEvent(self, event):
        if self.scope is not None:
            self.scope.close()
        super().closeEvent(event)
