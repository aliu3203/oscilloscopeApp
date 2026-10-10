import pyqtgraph as pg
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

DEFAULT_IP = "10.0.0.237"


class MainWindow(QMainWindow):
    """Layout only; no signals are connected yet."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Oscilloscope Capture")
        self.resize(1100, 700)

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
        self.channel_boxes = [QCheckBox(f"CH{i}") for i in range(1, 5)]
        self.capture_btn = QPushButton("Capture")
        self.save_btn = QPushButton("Save…")
        self.capture_btn.setEnabled(False)
        self.save_btn.setEnabled(False)

        channels_layout = QVBoxLayout()
        for box in self.channel_boxes:
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
