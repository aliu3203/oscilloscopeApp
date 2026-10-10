"""Doublet PMQ alignment helper. Start the Oscilloscope Capture app and connect it first, then run:

    python -m translate_align
"""

import sys

from PySide6.QtWidgets import QApplication

from translate_align.window import AlignWindow


def main():
    app = QApplication(sys.argv)
    window = AlignWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
