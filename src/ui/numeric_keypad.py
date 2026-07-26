"""
numeric_keypad.py

Self-contained on-screen numeric keypad, replacing reliance on the OS
virtual keyboard (squeekboard) for the initial-position fields
(connection_screen.py's pos_x_input/pos_y_input/pos_angle_input) — those
fields are purely numeric (decimal, possibly negative), so a full
QWERTY on-screen keyboard was never actually needed there, and the OS
keyboard's show/hide integration proved unreliable to get right blind
(no way to observe the real Wayland compositor from this dev
environment). This widget has no dependency on the platform's input
method stack at all.
"""

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import (
    QApplication, QWidget, QGridLayout, QHBoxLayout, QPushButton, QLineEdit
)

from src.ui.style import (
    BUTTON_STYLE_COMPACT, BUTTON_STYLE_PRIMARY_COMPACT, LAYOUT_SPACING,
)
from src.ui.theme_manager import ThemeManager


class NumericKeypad(QWidget):
    """
    A small popup-like panel that types into whichever QLineEdit it was
    last shown for (show_for()). Deliberately a PLAIN child widget, not
    a Qt.Popup top-level window: an earlier Qt.Popup-based version
    stopped reopening after a handful of show/close cycles — each
    Qt.Popup show/hide creates/destroys its own xdg_popup surface under
    Wayland, and the compositor here got out of sync after repeated
    cycles. As a normal child widget, show()/hide() only ever toggles
    visibility within the SAME window surface, so this class implements
    its own "tap outside closes it" behavior (an app-wide event filter)
    instead of relying on Qt.Popup's built-in one.

    Usage: one instance per screen that needs it, created with that
    screen as `parent` (so its own coordinate space can be used for
    positioning), shown on demand via show_for(line_edit).
    """

    def __init__(self, parent: QWidget, theme_manager: ThemeManager):
        super().__init__(parent)
        self._target: QLineEdit | None = None
        self._build_ui()
        self.hide()
        QApplication.instance().installEventFilter(self)
        theme_manager.theme_changed.connect(self._apply_theme)

    def _build_ui(self):
        layout = QGridLayout(self)
        layout.setSpacing(LAYOUT_SPACING // 3)

        digit_rows = [
            ["7", "8", "9"],
            ["4", "5", "6"],
            ["1", "2", "3"],
            ["±", "0", "."],
        ]
        for row, digits in enumerate(digit_rows):
            for col, label in enumerate(digits):
                btn = QPushButton(label)
                btn.setStyleSheet(BUTTON_STYLE_COMPACT)
                btn.clicked.connect(lambda checked=False, d=label: self._on_key(d))
                layout.addWidget(btn, row, col)

        bottom_row = QHBoxLayout()
        backspace_btn = QPushButton("⌫")
        backspace_btn.setStyleSheet(BUTTON_STYLE_COMPACT)
        backspace_btn.clicked.connect(self._on_backspace)
        self._accept_btn = QPushButton("Aceptar")
        self._accept_btn.setStyleSheet(BUTTON_STYLE_PRIMARY_COMPACT())
        self._accept_btn.clicked.connect(self.hide)
        bottom_row.addWidget(backspace_btn)
        bottom_row.addWidget(self._accept_btn)
        layout.addLayout(bottom_row, len(digit_rows), 0, 1, 3)

    def _apply_theme(self, _name: str) -> None:
        self._accept_btn.setStyleSheet(BUTTON_STYLE_PRIMARY_COMPACT())

    def show_for(self, line_edit: QLineEdit) -> None:
        """
        Show the keypad just BELOW `line_edit` (or above it, if there
        isn't room below within the parent screen) — anchored to the
        field itself rather than a fixed screen position, so it never
        covers the field being filled.
        """
        self._target = line_edit
        self.adjustSize()
        parent = self.parentWidget()

        below = line_edit.mapTo(parent, line_edit.rect().bottomLeft())
        x, y = below.x(), below.y() + 6
        max_x = max(parent.width() - self.width(), 0)
        max_y = max(parent.height() - self.height(), 0)
        if y > max_y:
            above = line_edit.mapTo(parent, line_edit.rect().topLeft())
            y = max(above.y() - self.height() - 6, 0)
        x = min(max(x, 0), max_x)

        self.move(x, y)
        self.show()
        self.raise_()

    def _on_key(self, key: str) -> None:
        if self._target is None:
            return
        if key == "±":
            self._toggle_sign()
            return
        if key == "." and "." in self._target.text():
            return  # only one decimal point — guard even though a
                     # validator on the field is also expected to reject it
        self._target.insert(key)

    def _on_backspace(self) -> None:
        if self._target is not None:
            self._target.backspace()

    def _toggle_sign(self) -> None:
        text = self._target.text()
        self._target.setText(text[1:] if text.startswith("-") else "-" + text)

    def eventFilter(self, watched, event) -> bool:
        """
        App-wide: hides the keypad on any tap that lands outside both
        the keypad itself and the field it's currently editing — see
        the class docstring for why this replaces Qt.Popup's built-in
        "click outside closes it" behavior instead of relying on it.
        """
        if event.type() == QEvent.MouseButtonPress and self.isVisible():
            tapped = QApplication.widgetAt(event.globalPosition().toPoint())
            is_on_keypad = tapped is not None and (tapped is self or self.isAncestorOf(tapped))
            is_on_target = tapped is self._target
            if not is_on_keypad and not is_on_target:
                self.hide()
        return super().eventFilter(watched, event)
