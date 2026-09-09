"""
device_error_dialog.py

Non-modal dialog shown whenever the ESP32 reports an unsolicited ERROR
(StateMachineBridge.device_error, fired from
SystemStateMachine.on_device_error — see system_state.py). Replaces
relying on a status-label line alone, which is easy to miss (the
operator may be at the physical rig, not looking at the screen) and
gets overwritten by the very IDLE state_changed the same error
triggers.

Deliberately non-modal (setModal(False), .show() not .exec()):
dismissing it must never be a precondition for using the rest of the
app (e.g. retrying the move right away). One instance is reused per
screen via show_device_error() rather than piling up a new window per
error.

Every code below already falls the state machine back to IDLE without
touching `_homed` (see SystemStateMachine._on_device_error) — none of
the errors currently defined in docs/protocol.md require re-HOME to
recover, only an actual lost connection does (handled separately via
StateMachineBridge.disconnected, a different signal that never reaches
this dialog). If a future error code IS meant to signal something
HOME-invalidating, add it to _CODES below with its own wording rather
than assuming the blanket reassurance at the bottom of this dialog
still holds for it.
"""

from typing import Optional

from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from src.ui.style import (
    BUTTON_STYLE_PRIMARY, COLOR_TEXT, FONT_SIZE_NORMAL, LAYOUT_MARGIN,
    LAYOUT_SPACING, STATUS_COLORS,
)

# code -> (plain-language explanation, suggested next step). Mirrors
# the codes documented in docs/protocol.md's "Errores" section — keep
# in sync if that list changes. "" is the bare `ERROR` (no
# ":<code>:<message>" suffix) the real firmware can send for a
# not-yet-specific-coded rejection (see protocol.py RESP_ERROR_PREFIX).
_CODES = {
    "LIMIT_REACHED": (
        "El eje llegó a su límite físico durante el movimiento.",
        "Revisa la posición objetivo o la trayectoria y vuelve a intentar.",
    ),
    "INVALID_STATE": (
        "El ESP32 rechazó el comando por no corresponder a su estado "
        "actual (una orden fuera de secuencia).",
        "Vuelve a intentar la acción desde el estado actual de la app.",
    ),
    "TYPE_MISMATCH": (
        "El tipo de trayectoria (con/sin tiempo) no coincidió con la que "
        "el ESP32 tenía almacenada.",
        "Vuelve a cargar y enviar el ensayo antes de correr.",
    ),
    "POINT_INDEX_MISMATCH": (
        "Se perdió la sincronía de puntos durante la transferencia de la "
        "trayectoria.",
        "Vuelve a cargar y enviar el ensayo.",
    ),
    "POINT_COUNT_MISMATCH": (
        "No todos los puntos de la trayectoria llegaron al ESP32.",
        "Vuelve a cargar y enviar el ensayo.",
    ),
    "UNKNOWN_COMMAND": (
        "El firmware conectado no reconoce este comando — posible "
        "versión de firmware distinta a la esperada por la app.",
        "Verifica que el ESP32 tenga el firmware correcto antes de "
        "continuar.",
    ),
    "": (
        "El ESP32 reportó un error sin código ni mensaje adicional.",
        "Revisa logs/gaitsim.log para más contexto y vuelve a intentar.",
    ),
}

_DEFAULT = (
    "El ESP32 reportó un error no documentado en el protocolo actual.",
    "Revisa logs/gaitsim.log para más contexto y vuelve a intentar.",
)


class DeviceErrorDialog(QDialog):
    """One instance reused per screen — see show_device_error()."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Error del ESP32")
        self.setModal(False)

        root = QVBoxLayout(self)
        root.setSpacing(LAYOUT_SPACING)
        root.setContentsMargins(
            LAYOUT_MARGIN, LAYOUT_MARGIN, LAYOUT_MARGIN, LAYOUT_MARGIN
        )

        self._raw_label = QLabel()
        self._raw_label.setStyleSheet(
            f"font-size: {FONT_SIZE_NORMAL}px; font-weight: bold; "
            f"color: {STATUS_COLORS()['ERROR']};"
        )
        self._raw_label.setWordWrap(True)
        root.addWidget(self._raw_label)

        self._explanation_label = QLabel()
        self._explanation_label.setStyleSheet(
            f"font-size: {FONT_SIZE_NORMAL}px; color: {COLOR_TEXT()};"
        )
        self._explanation_label.setWordWrap(True)
        root.addWidget(self._explanation_label)

        self._suggestion_label = QLabel()
        self._suggestion_label.setStyleSheet(
            f"font-size: {FONT_SIZE_NORMAL}px; color: {COLOR_TEXT()}; "
            f"font-style: italic;"
        )
        self._suggestion_label.setWordWrap(True)
        root.addWidget(self._suggestion_label)

        reassurance = QLabel(
            "El sistema volvió a IDLE. La calibración (HOME) sigue "
            "siendo válida — no es necesario recalibrar para continuar."
        )
        reassurance.setStyleSheet(
            f"font-size: {FONT_SIZE_NORMAL}px; color: {STATUS_COLORS()['IDLE']};"
        )
        reassurance.setWordWrap(True)
        root.addWidget(reassurance)

        button_row = QHBoxLayout()
        button_row.addStretch()
        ok_button = QPushButton("Entendido")
        ok_button.setStyleSheet(BUTTON_STYLE_PRIMARY())
        ok_button.clicked.connect(self.close)
        button_row.addWidget(ok_button)
        root.addLayout(button_row)

        self.setFixedWidth(440)

    def set_error(self, code: str, message: str) -> None:
        explanation, suggestion = _CODES.get(code, _DEFAULT)
        label = code if code else "(sin código)"
        self._raw_label.setText(f"ERROR [{label}]: {message or '(sin mensaje)'}")
        self._explanation_label.setText(explanation)
        self._suggestion_label.setText(f"Sugerencia: {suggestion}")


def show_device_error(owner, attr_name: str, parent, code: str, message: str) -> None:
    """
    Shows (creating on first use) a single DeviceErrorDialog reused
    across calls, stored as `attr_name` on `owner` (typically the
    calling screen's `self`) — so repeated errors update the same
    window instead of piling up new ones on top of each other.
    """
    dialog: Optional[DeviceErrorDialog] = getattr(owner, attr_name, None)
    if dialog is None:
        dialog = DeviceErrorDialog(parent)
        setattr(owner, attr_name, dialog)
    dialog.set_error(code, message)
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
