"""
Manual/offline verification for a new restriction in trajectory_screen.py:
Run must stay blocked until a CSV has been loaded AND sent (Load &&
Send) for the CURRENT initial position — closing a gap where, after
setting up a new trial's position (first GOTO after HOME, or "Elegir
Otro Ensayo" -> a new GOTO), Run stayed enabled even though no
trajectory had ever been sent for that position (either none at all,
or a stale one left over from a previous trial).

Tracked via `_ensayo_sent_for_position` (see trajectory_screen.py's
__init__): set to the position `_ensayo_trajectory` was last
successfully sent for; Run is only enabled while that still equals
`_position_session.position`. "Reiniciar Ensayo" is unaffected — it
never changes the position, and re-sends _ensayo_trajectory itself.

Uses a mocked ESP32Controller — no real hardware/serial connection
needed.

Usage:
    QT_QPA_PLATFORM=offscreen python3 -m tests.manual_test_run_requires_fresh_ensayo
"""

import sys
from unittest.mock import MagicMock

from PySide6.QtWidgets import QApplication

from src.communication.esp32_controller import ESP32Controller, TrajectoryTransferResult
from src.communication.protocol import Position, TrajectoryPoint
from src.controllers.initial_position_session import InitialPositionSession
from src.controllers.system_state import CalibrationSpace, SystemState, SystemStateMachine
from src.ui.bridge import StateMachineBridge
from src.ui.screens.trajectory_screen import TrajectoryScreen
from src.ui.theme_manager import ThemeManager

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


def make_state_machine():
    controller = MagicMock(spec=ESP32Controller)
    controller.is_connected = True
    controller.get_position.return_value = Position(x=0.0, y=0.0, angle=0.0)
    controller.send_trajectory.return_value = TrajectoryTransferResult(
        success=True, points_acknowledged=999
    )
    sm = SystemStateMachine(controller)
    sm._homed = True
    sm._state = SystemState.IDLE
    sm.last_calibration_space = SPACE
    return sm, controller


def make_trajectory_screen(sm: SystemStateMachine, position=None) -> TrajectoryScreen:
    bridge = StateMachineBridge(sm)
    session = InitialPositionSession(position=position)
    return TrajectoryScreen(bridge, session, ThemeManager())


def test_run_disabled_before_any_csv_sent():
    sm, _ = make_state_machine()
    screen = make_trajectory_screen(sm, Position(x=0.0, y=0.0, angle=0.0))
    screen._refresh_controls()
    check(
        "Run is disabled before any ensayo has been loaded/sent",
        not screen.run_button.isEnabled(),
    )


def test_run_enabled_after_successful_send():
    sm, _ = make_state_machine()
    screen = make_trajectory_screen(sm, Position(x=0.0, y=0.0, angle=0.0))
    screen._ensayo_trajectory = [TrajectoryPoint(t=0.0, x=0.0, y=0.0, angle=0.0)]
    screen._send_and_check()  # mirrors what "Load && Send" does
    screen._refresh_controls()
    check(
        "Run is enabled once the ensayo has been sent for the current position",
        screen.run_button.isEnabled(),
    )


def test_run_disabled_again_after_position_changes():
    """
    The exact scenario reported: after "Elegir Otro Ensayo" sets up a
    NEW initial position for a different trial, Run must not stay
    enabled from the OLD trial's send — the operator is expected to
    pick and load a new CSV for this position.
    """
    sm, _ = make_state_machine()
    screen = make_trajectory_screen(sm, Position(x=0.0, y=0.0, angle=0.0))
    screen._ensayo_trajectory = [TrajectoryPoint(t=0.0, x=0.0, y=0.0, angle=0.0)]
    screen._send_and_check()
    screen._refresh_controls()
    check("sanity: Run enabled right after send", screen.run_button.isEnabled())

    # Simulates ConnectionScreen._on_goto_succeeded for a NEW trial.
    screen._position_session.set(Position(x=5.0, y=5.0, angle=0.0))
    screen._refresh_controls()
    check(
        "Run is disabled again after the initial position changes",
        not screen.run_button.isEnabled(),
    )
    check(
        "tooltip explains why Run is disabled",
        "csv" in screen.run_button.toolTip().lower()
        or "ensayo" in screen.run_button.toolTip().lower(),
    )


def test_run_disabled_immediately_on_loading_new_csv_before_resend():
    sm, _ = make_state_machine()
    screen = make_trajectory_screen(sm, Position(x=0.0, y=0.0, angle=0.0))
    screen._ensayo_trajectory = [TrajectoryPoint(t=0.0, x=0.0, y=0.0, angle=0.0)]
    screen._send_and_check()
    screen._refresh_controls()
    check("sanity: Run enabled after first send", screen.run_button.isEnabled())

    # Mirrors _on_send_clicked loading a DIFFERENT file, before it's sent.
    screen._ensayo_trajectory = [TrajectoryPoint(t=0.0, x=1.0, y=1.0, angle=0.0)]
    screen._ensayo_sent_for_position = None
    screen._refresh_controls()
    check(
        "Run is disabled again as soon as a new CSV is loaded, before resending",
        not screen.run_button.isEnabled(),
    )

    screen._send_and_check()
    screen._refresh_controls()
    check("Run re-enables once the new CSV is actually sent", screen.run_button.isEnabled())


def test_restart_trial_unaffected_by_the_new_restriction():
    """
    "Reiniciar Ensayo" never changes the initial position (it restores
    TO the same target) and re-sends _ensayo_trajectory itself via
    _send_and_check() — this new restriction must not require picking
    a new CSV for that flow.
    """
    sm, controller = make_state_machine()
    target = Position(x=0.0, y=0.0, angle=0.0)
    screen = make_trajectory_screen(sm, target)
    screen._ensayo_trajectory = [TrajectoryPoint(t=0.0, x=0.0, y=0.0, angle=0.0)]
    screen._send_and_check()
    screen._refresh_controls()
    check("sanity: Run enabled before restart", screen.run_button.isEnabled())

    sm._state = SystemState.PAUSED
    sm.abort = lambda: None
    sm.safe_return_to_position = lambda *a, **k: None
    sm.run = lambda: None

    screen._on_restart_trial_clicked()
    screen._worker.wait()
    QApplication.instance().processEvents()
    sm._state = SystemState.IDLE  # sm.run was stubbed, so simulate its effect
    screen._refresh_controls()
    check(
        "Run stays enabled after Reiniciar Ensayo (no new CSV required)",
        screen.run_button.isEnabled(),
    )


def main():
    app = QApplication.instance() or QApplication(sys.argv)

    test_run_disabled_before_any_csv_sent()
    test_run_enabled_after_successful_send()
    test_run_disabled_again_after_position_changes()
    test_run_disabled_immediately_on_loading_new_csv_before_resend()
    test_restart_trial_unaffected_by_the_new_restriction()

    print()
    if _failures:
        print(f"{_failures} check(s) FAILED.")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
