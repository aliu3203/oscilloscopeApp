"""Roll matching of the PMQ doublet. Start the Oscilloscope Capture app and connect it first, then run:

    python -m roll_align
"""

import sys

from PySide6.QtWidgets import QApplication

from roll_align.window import RollWindow


def main():
    app = QApplication(sys.argv)
    window = RollWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
