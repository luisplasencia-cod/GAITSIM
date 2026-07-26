"""
status_indicator.py

The unified ESP32 connect/status control shown in the nav bar
(main_window.py) — replaces the previous pair of separate elements (a
"Conectar" button in ConnectionScreen + a small colored "foquito"
indicator here). See docs: 2026-07-25+ design pass, Luis's explicit
request.

Behavior:
    - While disconnected: acts as the connect button — clicking it
      triggers the exact same ESP32Controller.connect() call the old
      dedicated button made (see connection_screen.py's former
      _on_connect_clicked, now removed).
    - Once connected (or mid-action): pure indicator, same color-per-
      SystemState mapping the old StatusIndicator used (STATUS_COLORS).
      Clicking it does nothing — there is no manual "disconnect" action
      anywhere in this app today (only app-exit or an unexpected link
      loss disconnect), and Luis's explicit choice was not to add one
      here either, so this button stays a pure indicator once connected.
"""

from PySide6.QtWidgets import QPushButton

from src.ui.bridge import StateMachineBridge
from src.ui.style import CONNECTION_STATUS_BUTTON_STYLE, STATUS_COLORS
from src.ui.theme_manager import ThemeManager

# One-word Spanish label per SystemState name (plus "ERROR"), shown on
# the button itself — kept to a single word per Luis's explicit request
# so it reads at a glance from a normal working distance, same spirit as
# every other status text in this app.
_STATE_LABELS = {
    "DISCONNECTED": "Conectar",
    "CONNECTED": "Conectado",
    "IDLE": "Listo",
    "HOMING": "Calibrando",
    "RECEIVING_TRAJECTORY": "Enviando",
    "RUNNING": "Ejecutando",
    "PAUSED": "Pausado",
    "ERROR": "Error",
}


class ConnectionStatusButton(QPushButton):
    """A single button that is the connect trigger before a connection
    exists, and a color-coded state indicator afterward."""

    def __init__(self, bridge: StateMachineBridge, theme_manager: ThemeManager, parent=None):
        super().__init__(parent)
        self._bridge = bridge
        self._current_state_name = "DISCONNECTED"

        self.clicked.connect(self._on_clicked)
        self._apply_state("DISCONNECTED")

        self._bridge.state_changed.connect(self._on_state_changed)
        self._bridge.device_error.connect(self._on_device_error)
        self._bridge.disconnected.connect(self._on_disconnected)
        self._bridge.connected.connect(self._on_connected)
        # The state itself hasn't changed, only the palette — re-run
        # _apply_state() for whatever state is CURRENTLY shown so the
        # button's color updates immediately instead of waiting for the
        # next real state transition.
        theme_manager.theme_changed.connect(lambda _name: self._apply_state(self._current_state_name))

    def _on_clicked(self):
        controller = self._bridge.state_machine.controller
        if controller.is_connected:
            return  # pure indicator once connected — see module docstring
        controller.connect()
        self._bridge.notify_connected()

    def _on_connected(self):
        self._apply_state("CONNECTED")

    def _on_state_changed(self, state_name: str):
        self._apply_state(state_name)

    def _on_device_error(self, code: str, message: str):
        self.setToolTip(f"ERROR [{code}]: {message}")
        self._apply_state("ERROR")

    def _on_disconnected(self):
        self._apply_state("DISCONNECTED")

    def _apply_state(self, state_name: str):
        self._current_state_name = state_name
        status_colors = STATUS_COLORS()
        color = status_colors.get(state_name, status_colors["DISCONNECTED"])
        self.setText(_STATE_LABELS.get(state_name, state_name))
        self.setToolTip(state_name)
        self.setStyleSheet(CONNECTION_STATUS_BUTTON_STYLE().replace("{color}", color))
