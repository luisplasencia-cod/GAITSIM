"""
main.py

Application entry point.
"""

import sys
from PySide6.QtWidgets import QApplication

from src.ui.main_window import MainWindow
from src.ui.style import APP_STYLESHEET
from src.ui.theme_manager import ThemeManager
from src.ui.touch_dismiss_filter import TouchKeyboardDismissFilter

SERIAL_PORT = "/dev/ttyUSB0"  # adjust if your device enumerates differently


def main():
    app = QApplication(sys.argv)

    # Owns the live light/dark theme choice (2026-07-26+, Luis's
    # request) — re-applying the app-wide stylesheet here on every
    # theme_changed handles every widget that DOESN'T also bake a color
    # into its own explicit setStyleSheet() call (see style.py's module
    # docstring); MainWindow wires up the rest (nav bar, per-screen
    # buttons) via the same signal.
    theme_manager = ThemeManager()
    app.setStyleSheet(APP_STYLESHEET())
    theme_manager.theme_changed.connect(lambda _name: app.setStyleSheet(APP_STYLESHEET()))

    # Kept as a local var referenced by the app itself (not just this
    # function's stack) so it isn't garbage-collected — installEventFilter
    # does not itself keep the filter object alive.
    keyboard_dismiss_filter = TouchKeyboardDismissFilter(app)
    app.installEventFilter(keyboard_dismiss_filter)

    window = MainWindow(serial_port=SERIAL_PORT, theme_manager=theme_manager)
    # Always kiosk/fullscreen (2026-07-25+ request) — no windowed mode.
    window.showFullScreen()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()