from pathlib import Path

import pyqtgraph as pg
from PySide6.QtCore import QSettings, QThreadPool, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
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
from scope.api_server import ApiServer
from scope.siglent import CHANNELS, SiglentScope
from scope.storage import default_filename, save_traces

DEFAULT_IP = "10.0.0.237"
SAVE_FILTERS = {"CSV (*.csv)": ".csv", "NumPy archive (*.npz)": ".npz"}

# Roughly the scope's own channel colors, darkened to be readable on a white background.
CHANNEL_COLORS = {"C1": "#b8960b", "C2": "#d6249f", "C3": "#008b9a", "C4": "#1f4fd1"}


class MainWindow(QMainWindow):
    # Emitted from an API server thread; Qt queues it to the GUI thread.
    api_captured = Signal(object)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Oscilloscope Capture")
        self.resize(1100, 700)

        self.scope = None
        self.traces = {}
        self._worker = None  # keep a reference so its signals outlive the pool's run()
        self._busy = False
        self.settings = QSettings("researchMusumeci", "OscilloscopeCapture")

        # Connection row
        self.ip_edit = QLineEdit(DEFAULT_IP)
        self.ip_edit.setFixedWidth(160)
        self.connect_btn = QPushButton("Connect")
        self.conn_label = QLabel("Not connected")
        self.api_label = QLabel()

        conn_row = QHBoxLayout()
        conn_row.addWidget(QLabel("Scope IP:"))
        conn_row.addWidget(self.ip_edit)
        conn_row.addWidget(self.connect_btn)
        conn_row.addWidget(self.conn_label)
        conn_row.addStretch()
        conn_row.addWidget(self.api_label)

        # Left panel: channels + actions
        self.channel_boxes = {ch: QCheckBox(f"CH{ch[1:]}") for ch in CHANNELS}
        self.capture_btn = QPushButton("Capture")
        self.run_btn = QPushButton("Run")
        self.stop_btn = QPushButton("Stop")
        self.save_btn = QPushButton("Save…")
        self.capture_btn.setEnabled(False)
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)
        self.save_btn.setEnabled(False)

        channels_layout = QVBoxLayout()
        for box in self.channel_boxes.values():
            channels_layout.addWidget(box)
        channels_group = QGroupBox("Channels")
        channels_group.setLayout(channels_layout)

        left_panel = QVBoxLayout()
        left_panel.addWidget(channels_group)
        left_panel.addWidget(self.capture_btn)
        run_stop = QHBoxLayout()
        run_stop.addWidget(self.run_btn)
        run_stop.addWidget(self.stop_btn)
        left_panel.addLayout(run_stop)
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
        self.save_btn.clicked.connect(self.on_save_clicked)
        self.run_btn.clicked.connect(self.on_run_clicked)
        self.stop_btn.clicked.connect(self.on_stop_clicked)
        self.api_captured.connect(self.on_api_captured)

        self.api = ApiServer(get_scope=self._connected_scope, on_capture=self.api_captured.emit)
        try:
            self.api.start()
            self.api_label.setText(f"API: {self.api.url}")
        except OSError as e:
            self.api = None
            self.api_label.setText(f"API: off ({e.strerror})")

    # --- helpers ------------------------------------------------------------

    def _run(self, fn, on_finished, on_failed, *args, **kwargs):
        self._worker = Worker(fn, *args, **kwargs)
        self._worker.signals.finished.connect(on_finished)
        self._worker.signals.failed.connect(on_failed)
        QThreadPool.globalInstance().start(self._worker)

    def _connected_scope(self):
        # Called from API server threads.
        scope = self.scope
        return scope if scope is not None and scope.is_connected else None

    def _set_busy(self, busy):
        self._busy = busy
        connected = self.scope is not None and self.scope.is_connected
        self.connect_btn.setEnabled(not busy)
        self.ip_edit.setEnabled(not busy and not connected)
        self.capture_btn.setEnabled(not busy and connected)
        self.run_btn.setEnabled(not busy and connected)
        self.stop_btn.setEnabled(not busy and connected)
        self.save_btn.setEnabled(not busy and bool(self.traces))

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
        return self.scope.enabled_channels(), self.scope.trigger_mode()

    def on_connected(self, result):
        enabled, mode = result
        for ch, box in self.channel_boxes.items():
            box.setChecked(ch in enabled)
        self.connect_btn.setText("Disconnect")
        self.conn_label.setText(f"Connected: {self.scope.model}")
        self._set_busy(False)
        self.statusBar().showMessage(f"Connected to {self.scope.idn} (trigger mode: {mode})")

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

    def _show_traces(self, traces, source):
        self.traces = traces
        self.plot.clear()
        for ch, trace in traces.items():
            self.plot.plot(trace.time, trace.voltage, pen=pg.mkPen(CHANNEL_COLORS[ch], width=1), name=ch)
        self.plot.autoRange()
        n_points = ", ".join(sorted({str(len(t)) for t in traces.values()}))
        self.statusBar().showMessage(f"{source} {', '.join(traces)} ({n_points} pts each)")

    def on_captured(self, traces):
        if self.api is not None:
            self.api.last_traces = traces
        self._show_traces(traces, "Captured")
        self._set_busy(False)

    def on_api_captured(self, traces):
        self._show_traces(traces, "Captured via API:")
        self._set_busy(self._busy)  # refresh Save without disturbing a GUI action in progress

    def on_capture_failed(self, message):
        if self.scope is not None and not self.scope.is_connected:
            self._set_disconnected(message)
        else:
            self._set_busy(False)
            self.statusBar().showMessage(message)

    # --- run / stop ---------------------------------------------------------

    def on_run_clicked(self):
        self._set_busy(True)
        self._run(self.scope.run, self.on_mode_changed, self.on_capture_failed)

    def on_stop_clicked(self):
        self._set_busy(True)
        self._run(self.scope.stop, self.on_mode_changed, self.on_capture_failed)

    def on_mode_changed(self, mode):
        self._set_busy(False)
        self.statusBar().showMessage(f"Trigger mode: {mode}")

    # --- save ---------------------------------------------------------------

    def on_save_clicked(self):
        last_dir = self.settings.value("last_save_dir", str(Path.home()))
        suggested = str(Path(last_dir) / default_filename(self.traces))
        path, selected_filter = QFileDialog.getSaveFileName(
            self, "Save traces", suggested, ";;".join(SAVE_FILTERS)
        )
        if not path:
            return
        path = Path(path)
        if path.suffix.lower() not in SAVE_FILTERS.values():
            path = path.with_name(path.name + SAVE_FILTERS.get(selected_filter, ".csv"))

        self.settings.setValue("last_save_dir", str(path.parent))
        self._set_busy(True)
        self.statusBar().showMessage(f"Saving {path.name}…")
        self._run(save_traces, self.on_saved, self.on_save_failed, self.traces, path)

    def on_saved(self, path):
        self._set_busy(False)
        self.statusBar().showMessage(f"Saved {path}")

    def on_save_failed(self, message):
        self._set_busy(False)
        self.statusBar().showMessage(f"Save failed: {message}")

    def closeEvent(self, event):
        if self.api is not None:
            self.api.stop()
        if self.scope is not None:
            self.scope.close()
        super().closeEvent(event)
