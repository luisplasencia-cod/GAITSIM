"""
status_indicator.py

Small circular "foquito" widget showing the ESP32 link/system state at
a glance, independent of which screen is currently active. Pure Qt —
consumes only the signals StateMachineBridge already exposes, so it
needs no new plumbing in SystemStateMachine or below.
"""

from PySide6.QtWidgets import QLabel

from src.ui.bridge import StateMachineBridge
from src.ui.style import COLOR_INDICATOR_RING, STATUS_COLORS, STATUS_INDICATOR_DIAMETER


class StatusIndicator(QLabel):
    """
    A colored circle reflecting the current SystemState, with a
    transient red flash on device-reported errors (held until the next
    state change, since an error is usually followed by a state
    transition back to IDLE that would otherwise overwrite it silently).
    """

    def __init__(self, bridge: StateMachineBridge, parent=None):
        super().__init__(parent)
        self._bridge = bridge

        self.setFixedSize(STATUS_INDICATOR_DIAMETER, STATUS_INDICATOR_DIAMETER)
        self._set_color(STATUS_COLORS["DISCONNECTED"])
        self.setToolTip("DISCONNECTED")

        self._bridge.state_changed.connect(self._on_state_changed)
        self._bridge.device_error.connect(self._on_device_error)
        self._bridge.disconnected.connect(self._on_disconnected)
        self._bridge.connected.connect(self._on_connected)

    def _on_connected(self):
        self.setToolTip("CONNECTED (not homed)")
        self._set_color(STATUS_COLORS["CONNECTED"])

    def _on_state_changed(self, state_name: str):
        self.setToolTip(state_name)
        self._set_color(STATUS_COLORS.get(state_name, STATUS_COLORS["DISCONNECTED"]))

    def _on_device_error(self, code: str, message: str):
        self.setToolTip(f"ERROR [{code}]: {message}")
        self._set_color(STATUS_COLORS["ERROR"])

    def _on_disconnected(self):
        self.setToolTip("DISCONNECTED")
        self._set_color(STATUS_COLORS["DISCONNECTED"])

    def _set_color(self, color: str):
        radius = STATUS_INDICATOR_DIAMETER // 2
        self.setStyleSheet(
            f"background-color: {color}; "
            f"border-radius: {radius}px; "
            f"border: 2px solid {COLOR_INDICATOR_RING};"
        )
