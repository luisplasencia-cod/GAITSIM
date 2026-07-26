"""
theme_manager.py

Owns the app's single light/dark theme choice and broadcasts changes as
a Qt signal — the Qt-facing counterpart to style.py's plain
get_theme_name()/set_theme()/toggle_theme() functions (which have no Qt
dependency, same layering spirit as the rest of this codebase). Created
once in main.py, passed down to MainWindow like bridge/position_session,
and further down to any screen/widget that needs to re-apply its own
baked-in colors when the theme changes (see style.py's module docstring
for why a live re-application is needed per widget at all).

Not persisted across app restarts — always starts on style.py's default
(dark) each launch. Add persistence later if that's ever wanted; out of
scope here (not requested).
"""

from PySide6.QtCore import QObject, Signal

from src.ui import style


class ThemeManager(QObject):
    theme_changed = Signal(str)  # emits the new theme name ("dark"/"light")

    @property
    def name(self) -> str:
        return style.get_theme_name()

    def toggle(self) -> None:
        new_name = style.toggle_theme()
        self.theme_changed.emit(new_name)
