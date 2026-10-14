"""Doublet PMQ alignment by stage scans. Start the Oscilloscope Capture app and connect it first, then run:

    python -m scan_align
"""

import sys

from PySide6.QtWidgets import QApplication

from scan_align.window import ScanWindow


def main():
    app = QApplication(sys.argv)
    window = ScanWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
