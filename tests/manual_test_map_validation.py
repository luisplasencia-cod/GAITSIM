"""
Manual/offline verification for the calibration-space (map) validation
now enforced on every flow that can send a movement or trajectory to
the ESP32: manual moves, the synchronized initial/return trajectories,
and CSV-loaded ensayos (src/utils/trajectory_validator.py).

Uses a mocked ESP32Controller — no real hardware/serial connection
needed — so it can run anywhere, same spirit as the mocked-controller
verification already used elsewhere in this project's history.

Usage:
    python3 -m tests.manual_test_map_validation
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
from src.utils import trajectory_generator
from src.utils.trajectory_validator import PositionOutOfRangeError

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
    """A SystemStateMachine wired to a mocked ESP32Controller, already
    IDLE/homed/calibrated — bypasses the real home()/HOMING sweep since
    only the resulting CalibrationSpace matters for these checks."""
    controller = MagicMock(spec=ESP32Controller)
    controller.is_connected = True
    # Raw motor steps (2026-08-26: GET_POSITION carries steps, not
    # cm/deg — see docs/protocol.md, Consulta de Posición). 5cm/5cm/0deg
    # equivalent, using SystemStateMachine.STEPS_PER_CM_X/Y (400/800).
    controller.get_position.return_value = Position(x=2000.0, y=4000.0, angle=0.0)
    controller.send_trajectory.return_value = TrajectoryTransferResult(
        success=True, points_acknowledged=999
    )

    def fake_run():
        # Mirrors the test firmware's near-instantaneous simulated
        # completion, so _run_trajectory_blocking's finished.wait()
        # (used internally by move_relative) doesn't hang forever with
        # no real ESP32 to report FINISHED.
        callback = controller.on_trajectory_finished
        if callback is not None:
            callback()

    controller.run.side_effect = fake_run

    sm = SystemStateMachine(controller)
    sm._homed = True
    sm._state = SystemState.IDLE
    sm.last_calibration_space = SPACE
    return sm, controller


def test_manual_move_out_of_range():
    sm, controller = make_state_machine()
    try:
        sm.move_relative("Y", "+", 100.0)  # 5 + 100 = 105, way past y_max=40
        check("manual move out-of-range raises PositionOutOfRangeError", False)
    except PositionOutOfRangeError as exc:
        check("manual move out-of-range raises PositionOutOfRangeError", True)
        print(f"   message: {exc}")
    check("manual move out-of-range sends nothing to ESP32", not controller.send_trajectory.called)


def test_manual_move_within_range():
    sm, controller = make_state_machine()
    sm.move_relative("Y", "+", 2.0)  # 5 + 2 = 7, within [0, 40]
    check("manual move within range succeeds and sends trajectory", controller.send_trajectory.called)


def test_initial_position_out_of_range():
    try:
        trajectory_generator.generate_synchronized_trajectory(
            Position(x=0.0, y=100.0, angle=0.0), SPACE
        )
        check("initial position out-of-range raises PositionOutOfRangeError", False)
    except PositionOutOfRangeError as exc:
        check("initial position out-of-range raises PositionOutOfRangeError", True)
        print(f"   message: {exc}")


def test_return_trajectory_out_of_range():
    try:
        trajectory_generator.generate_safe_return_trajectory(
            current=Position(x=5.0, y=5.0, angle=0.0),
            target=Position(x=50.0, y=5.0, angle=0.0),  # x=50 past x_max=30
            floor_y=0.0,
            calibration_space=SPACE,
            lift_margin_cm=5.0,
            angle_reference_deg=0.0,
        )
        check("return trajectory out-of-range raises PositionOutOfRangeError", False)
    except PositionOutOfRangeError as exc:
        check("return trajectory out-of-range raises PositionOutOfRangeError", True)
        print(f"   message: {exc}")


def make_trajectory_screen(sm: SystemStateMachine) -> TrajectoryScreen:
    bridge = StateMachineBridge(sm)
    session = InitialPositionSession(position=Position(x=0.0, y=0.0, angle=0.0))
    return TrajectoryScreen(bridge, session, ThemeManager())


def test_ensayo_out_of_range():
    sm, controller = make_state_machine()
    screen = make_trajectory_screen(sm)
    screen._ensayo_trajectory = [
        TrajectoryPoint(t=0.0, x=0.0, y=0.0, angle=0.0),
        TrajectoryPoint(t=0.1, x=5.0, y=50.0, angle=0.0),  # y=50 past y_max=40
        TrajectoryPoint(t=0.2, x=35.0, y=10.0, angle=0.0),  # x=35 past x_max=30
    ]
    try:
        screen._send_and_check()
        check("out-of-range ensayo is rejected (whole trajectory)", False)
    except RuntimeError as exc:
        message = str(exc)
        check("out-of-range ensayo is rejected (whole trajectory)", "rechazado" in message.lower())
        check("rejection message names the offending points/axes", "Punto 1" in message and "Punto 2" in message)
        print(f"   message: {message}")
    check("out-of-range ensayo sends nothing to ESP32", not controller.send_trajectory.called)


def test_ensayo_within_range():
    sm, controller = make_state_machine()
    screen = make_trajectory_screen(sm)
    screen._ensayo_trajectory = [
        TrajectoryPoint(t=0.0, x=0.0, y=0.0, angle=0.0),
        TrajectoryPoint(t=0.1, x=5.0, y=5.0, angle=0.0),
    ]
    screen._send_and_check()
    check("in-range ensayo is sent to the ESP32", controller.send_trajectory.called)


def test_ensayo_without_calibration_data():
    sm, controller = make_state_machine()
    sm.last_calibration_space = None
    screen = make_trajectory_screen(sm)
    screen._ensayo_trajectory = [TrajectoryPoint(t=0.0, x=0.0, y=0.0, angle=0.0)]
    try:
        screen._send_and_check()
        check("ensayo without calibration data is rejected", False)
    except RuntimeError as exc:
        check("ensayo without calibration data is rejected", "calibraci" in str(exc).lower())
    check("no calibration data -> nothing sent to ESP32", not controller.send_trajectory.called)


def main():
    app = QApplication.instance() or QApplication(sys.argv)

    test_manual_move_out_of_range()
    test_manual_move_within_range()
    test_initial_position_out_of_range()
    test_return_trajectory_out_of_range()
    test_ensayo_out_of_range()
    test_ensayo_within_range()
    test_ensayo_without_calibration_data()

    print()
    if _failures:
        print(f"{_failures} check(s) FAILED.")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
