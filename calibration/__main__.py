"""Photodiode position calibration. Start the Oscilloscope Capture app and connect it first, then run:

    python -m calibration
"""

import sys

from PySide6.QtWidgets import QApplication

from calibration.window import CalibrationWindow


def main():
    app = QApplication(sys.argv)
    window = CalibrationWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
