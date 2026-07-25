"""
Manual/offline verification for the two UI-only improvements added on
top of the map-boundary validation (src/utils/trajectory_validator.py):

1. LimitViolationDialog (src/ui/limit_violation_dialog.py) — replaces
   plain status text with a modal warning reusing CalibrationMapView.
2. ConnectionScreen's "Estado del Sistema" box no longer grows/overflows
   with long status messages (_AutoFitLabel wraps + shrinks font size
   to keep the FULL message visible, plus a fixed-height group box).

Runs fully offscreen (QT_QPA_PLATFORM=offscreen), no real hardware.

Usage:
    QT_QPA_PLATFORM=offscreen python3 -m tests.manual_test_ui_improvements
"""

import sys
from unittest.mock import MagicMock

from PySide6.QtWidgets import QApplication

from src.communication.esp32_controller import ESP32Controller
from src.communication.protocol import Position
from src.controllers.initial_position_session import InitialPositionSession
from src.controllers.system_state import CalibrationSpace, SystemState, SystemStateMachine
from src.ui.bridge import StateMachineBridge
from src.ui.limit_violation_dialog import LimitViolationDialog
from src.ui.screens.connection_screen import ConnectionScreen
from src.utils.trajectory_validator import check_position

SPACE = CalibrationSpace(
    y_min=0.0, y_max=40.0, x_min=0.0, x_max=30.0, angle_min=-20.0, angle_max=20.0
)

_failures = 0


def check(label: str, condition: bool):
    global _failures
    status = "PASS" if condition else "FAIL"
    if not condition:
        _failures += 1
    print(f"{status}: {label}")


def test_dialog_sizes_without_overlap():
    """
    Regression guard for a real layout bug found this session: a fixed
    resize()/plain adjustSize() under-counted a word-wrapped QLabel's
    real height (before the dialog had ever been laid out at a real
    width), pushing the "Entendido y corregir" button down over the
    map. The fix computes height via layout().heightForWidth(width)
    AFTER fixing the width — this checks that the dialog's final size
    actually matches that computed height for both a short (one axis)
    and a long (three axes + an extra note) message.
    """
    cases = [
        ("single axis", Position(x=10.0, y=52.5, angle=0.0), ""),
        (
            "three axes + extra note",
            Position(x=37.0, y=45.0, angle=25.0),
            "y 3 punto(s) más fuera de rango",
        ),
    ]
    for label, position, extra_note in cases:
        violations = check_position(position, SPACE)
        dialog = LimitViolationDialog(
            SPACE, "Ensayo rechazado", position, violations, extra_note=extra_note
        )
        expected_height = dialog.layout().heightForWidth(dialog.width())
        check(
            f"dialog height matches heightForWidth ({label})",
            dialog.height() == expected_height,
        )
        # The map view's own hard floor (set in CalibrationMapView.__init__)
        # must still fit inside whatever the dialog ended up sizing to.
        check(
            f"dialog taller than the map's minimum height ({label})",
            dialog.height() >= dialog._map_view.minimumSize().height(),
        )


def test_dialog_highlights_correct_boundaries():
    position = Position(x=37.0, y=45.0, angle=25.0)  # X, Y, and angle all over max
    violations = check_position(position, SPACE)
    dialog = LimitViolationDialog(SPACE, "Ensayo rechazado", position, violations)
    boundaries = dialog._map_view._violation_boundaries
    check(
        "all 3 exceeded boundaries are flagged for highlighting",
        boundaries == {"x_max", "y_max", "angle_max"},
    )


def make_connection_screen():
    controller = MagicMock(spec=ESP32Controller)
    controller.is_connected = True
    sm = SystemStateMachine(controller)
    sm._state = SystemState.IDLE
    bridge = StateMachineBridge(sm)
    return ConnectionScreen(bridge, InitialPositionSession())


def test_status_box_fixed_height():
    screen = make_connection_screen()
    # Must be shown/laid out at a real size BEFORE exercising _refit()
    # below — otherwise the label's width()/height() are still
    # placeholder values from before layout, and the font-size check
    # can't tell whether shrinking actually happened.
    screen.resize(1280, 800)
    screen.show()
    QApplication.instance().processEvents()

    status_box = screen.status_label.parentWidget()
    height_before = status_box.height()

    screen.status_label.setText("OK")
    check("status box height unchanged after a short message", status_box.height() == height_before)

    long_message = (
        "Ensayo rechazado: 3 punto(s) de la trayectoria fuera del espacio "
        "calibrado: Punto 1 (t=0.1s): Y excede el máximo por 10.0cm; ajusta "
        "el valor a <= 40.0cm para quedar dentro del rango; Punto 2 "
        "(t=0.2s): X excede el máximo por 5.0cm"
    )
    screen.status_label.setText(long_message)
    check("status box height unchanged after a long message", status_box.height() == height_before)
    check(
        "long message is shown in FULL (wrapped/shrunk, not truncated)",
        screen.status_label.text() == long_message,
    )
    check(
        "font size was shrunk below the base size to make it fit",
        f"font-size: {screen.status_label._MAX_FONT_PX}px" not in screen.status_label.styleSheet(),
    )
    check(
        "full message is still available via tooltip",
        screen.status_label.toolTip() == long_message,
    )


def main():
    app = QApplication.instance() or QApplication(sys.argv)

    test_dialog_sizes_without_overlap()
    test_dialog_highlights_correct_boundaries()
    test_status_box_fixed_height()

    print()
    if _failures:
        print(f"{_failures} check(s) FAILED.")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
