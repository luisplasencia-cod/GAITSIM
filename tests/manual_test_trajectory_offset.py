"""
Manual/offline verification for a real bug fix in
trajectory_screen.py's _offset_points(): the angular axis used to jump
straight to an absolute value on Run instead of starting smoothly from
the rig's current angle, unlike X/Y.

Root cause: the old formula (`p.axis + position.axis`, no subtraction)
implicitly assumed the loaded CSV's first row was already
(x=0, y=0, angle=0). True in practice for X/Y (these trials are
recorded starting from that origin), so X/Y "already worked" — but NOT
true for angle, whose CSV values are real recorded joint angles that
rarely start at 0. Fix: subtract the trajectory's own first point on
every axis (X, Y, AND angle) before adding the current initial
position — a no-op for X/Y (first point already ~0) but fixes angle.

No hardware/Qt event loop needed for these checks — _offset_points is a
pure function of (loaded points, position_session.position).

Usage:
    QT_QPA_PLATFORM=offscreen python3 -m tests.manual_test_trajectory_offset
"""

import sys
from unittest.mock import MagicMock

from PySide6.QtWidgets import QApplication

from src.communication.esp32_controller import ESP32Controller
from src.communication.protocol import Position, TrajectoryPoint
from src.controllers.initial_position_session import InitialPositionSession
from src.controllers.system_state import SystemStateMachine
from src.ui.bridge import StateMachineBridge
from src.ui.screens.trajectory_screen import TrajectoryScreen
from src.ui.theme_manager import ThemeManager

_failures = 0


def check(label: str, condition: bool):
    global _failures
    status = "PASS" if condition else "FAIL"
    if not condition:
        _failures += 1
    print(f"{status}: {label}")


def make_screen(position) -> TrajectoryScreen:
    controller = MagicMock(spec=ESP32Controller)
    controller.is_connected = True
    sm = SystemStateMachine(controller)
    bridge = StateMachineBridge(sm)
    session = InitialPositionSession(position=position)
    return TrajectoryScreen(bridge, session, ThemeManager())


def test_angle_no_jump_current_zero_csv_negative():
    """The exact bug report: current angle 0, CSV's first angle -20 —
    must NOT jump straight to -20; must start exactly at the current
    angle (0) and follow the trajectory's shape from there."""
    screen = make_screen(Position(x=0.0, y=0.0, angle=0.0))
    csv_points = [
        TrajectoryPoint(t=0.0, x=0.0, y=0.0, angle=-20.0),
        TrajectoryPoint(t=0.1, x=0.0, y=0.0, angle=-15.0),
        TrajectoryPoint(t=0.2, x=0.0, y=0.0, angle=-10.0),
    ]
    offset = screen._offset_points(csv_points)
    check("first sent angle equals the CURRENT angle (no jump)", offset[0].angle == 0.0)
    check(
        "trajectory shape preserved (each step still +5deg)",
        offset[1].angle == 5.0 and offset[2].angle == 10.0,
    )


def test_angle_no_jump_various_current_and_csv_start():
    """Sweeps a few (current angle, csv first angle) combinations per
    the task's explicit test request (0, 10, -15)."""
    cases = [
        (0.0, -20.0),
        (10.0, -20.0),
        (-15.0, 5.0),
        (10.0, 10.0),  # already aligned — offset should be a no-op
    ]
    for current_angle, csv_first_angle in cases:
        screen = make_screen(Position(x=0.0, y=0.0, angle=current_angle))
        csv_points = [
            TrajectoryPoint(t=0.0, x=0.0, y=0.0, angle=csv_first_angle),
            TrajectoryPoint(t=0.1, x=0.0, y=0.0, angle=csv_first_angle + 5.0),
        ]
        offset = screen._offset_points(csv_points)
        check(
            f"current={current_angle:g}, csv_first={csv_first_angle:g}: "
            f"no jump (first sent == current)",
            offset[0].angle == current_angle,
        )
        check(
            f"current={current_angle:g}, csv_first={csv_first_angle:g}: "
            f"shape preserved (+5deg step)",
            offset[1].angle == current_angle + 5.0,
        )


def test_xy_behavior_unchanged_when_csv_starts_at_origin():
    """X/Y "already worked" specifically because these trials are
    recorded starting at (x=0, y=0) — confirms the generalized formula
    is a no-op for that real-world case, i.e. no regression."""
    screen = make_screen(Position(x=12.5, y=8.5, angle=0.0))
    csv_points = [
        TrajectoryPoint(t=0.0, x=0.0, y=0.0, angle=0.0),
        TrajectoryPoint(t=0.1, x=1.0, y=2.0, angle=0.0),
    ]
    offset = screen._offset_points(csv_points)
    check("X unchanged: first point lands exactly on position.x", offset[0].x == 12.5)
    check("Y unchanged: first point lands exactly on position.y", offset[0].y == 8.5)
    check("X shape preserved", offset[1].x == 13.5)
    check("Y shape preserved", offset[1].y == 10.5)


def test_xy_also_fixed_when_csv_does_not_start_at_origin():
    """If a CSV's X/Y ever doesn't start at 0 either, the same fix
    applies there too — not just angle."""
    screen = make_screen(Position(x=0.0, y=0.0, angle=0.0))
    csv_points = [
        TrajectoryPoint(t=0.0, x=3.0, y=-2.0, angle=0.0),
        TrajectoryPoint(t=0.1, x=4.0, y=-1.0, angle=0.0),
    ]
    offset = screen._offset_points(csv_points)
    check("X no jump even when CSV doesn't start at 0", offset[0].x == 0.0)
    check("Y no jump even when CSV doesn't start at 0", offset[0].y == 0.0)
    check("X shape preserved", offset[1].x == 1.0)
    check("Y shape preserved", offset[1].y == 1.0)


def test_no_position_session_passthrough():
    screen = make_screen(None)
    csv_points = [TrajectoryPoint(t=0.0, x=1.0, y=2.0, angle=-20.0)]
    offset = screen._offset_points(csv_points)
    check("no initial position -> points returned unchanged", offset == csv_points)


def test_empty_trajectory_no_crash():
    screen = make_screen(Position(x=0.0, y=0.0, angle=0.0))
    offset = screen._offset_points([])
    check("empty trajectory -> empty result, no crash", offset == [])


def main():
    app = QApplication.instance() or QApplication(sys.argv)

    test_angle_no_jump_current_zero_csv_negative()
    test_angle_no_jump_various_current_and_csv_start()
    test_xy_behavior_unchanged_when_csv_starts_at_origin()
    test_xy_also_fixed_when_csv_does_not_start_at_origin()
    test_no_position_session_passthrough()
    test_empty_trajectory_no_crash()

    print()
    if _failures:
        print(f"{_failures} check(s) FAILED.")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
