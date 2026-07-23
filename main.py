"""
main.py

Application entry point.
"""

import sys
from PySide6.QtWidgets import QApplication

from src.ui.main_window import MainWindow
from src.ui.style import APP_STYLESHEET

SERIAL_PORT = "/dev/ttyUSB0"  # adjust if your device enumerates differently


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(APP_STYLESHEET)
    window = MainWindow(serial_port=SERIAL_PORT)
    window.resize(1280, 800)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()