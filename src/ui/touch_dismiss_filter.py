"""
touch_dismiss_filter.py

Application-wide event filter that closes the on-screen keyboard
(squeekboard, via the Wayland text-input protocol) when the operator
taps outside the currently focused text field.

There is no keyboard-managing code anywhere else in this app — the
keyboard is an OS-level component that shows/hides itself automatically
based on Qt's own input-method focus state (a QLineEdit gaining focus
marks a text input as active; losing focus marks it inactive). It was
staying open because tapping empty space in Qt does not, by itself,
clear focus from whatever QLineEdit last had it — this filter is the
missing piece, not a keyboard implementation of its own.
"""

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QApplication, QLineEdit


class TouchKeyboardDismissFilter(QObject):
    """Install once, application-wide (see main.py), via
    QApplication.installEventFilter(). On every mouse press, if the
    tapped widget is not a text field, clears focus from whatever
    QLineEdit currently holds it — which is what actually tells the
    input method (and therefore the on-screen keyboard) to hide.

    Deliberately does NOT also call QGuiApplication.inputMethod().hide()
    directly (an earlier version did): that explicit call turned out to
    suppress the keyboard from reappearing on the NEXT field tap too —
    clearFocus() alone is the canonical Qt signal ("no text-input-
    enabled widget is focused") that every input method (including
    squeekboard, via qtwayland's text-input protocol) already reacts to
    automatically for both hiding AND the following show, same as any
    other Qt app with no keyboard-specific code at all."""

    def eventFilter(self, watched, event) -> bool:
        if event.type() == QEvent.MouseButtonPress:
            focused = QApplication.focusWidget()
            if isinstance(focused, QLineEdit):
                tapped = QApplication.widgetAt(event.globalPosition().toPoint())
                if not isinstance(tapped, QLineEdit):
                    focused.clearFocus()
        return super().eventFilter(watched, event)
