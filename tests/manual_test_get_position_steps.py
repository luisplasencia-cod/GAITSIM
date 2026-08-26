"""
Manual/offline verification for the 2026-08-26 change to GET_POSITION
(see docs/protocol.md, Consulta de Posición): the wire response now
carries raw motor step counts for all 3 axes (X, Y, Angular), not
cm/deg — same treatment already applied to the calibration sweep's
LIM{AXIS}MAX. SystemStateMachine.get_position() is the one place that
converts back to cm/deg; ESP32Controller.get_position() (the raw wire
layer) and every OTHER internal caller of it must never be used
directly for real-unit math, or steps get treated as if already cm/deg.

Uses a mocked ESP32Controller — no real hardware/serial connection
needed — same spirit as the mocked-controller verification already
used elsewhere in this project's history.

Usage:
    python3 -m tests.manual_test_get_position_steps
"""

from unittest.mock import MagicMock

from src.communication.esp32_controller import ESP32Controller
from src.communication.protocol import Position
from src.controllers.system_state import SystemState, SystemStateMachine

_failures = 0


def check(label: str, condition: bool):
    global _failures
    status = "PASS" if condition else "FAIL"
    if not condition:
        _failures += 1
    print(f"{status}: {label}")


def approx(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) < tol


def main():
    controller = MagicMock(spec=ESP32Controller)
    controller.is_connected = True
    # Raw steps: X=2000 (5cm @ 400 steps/cm), Y=4000 (5cm @ 800 steps/cm),
    # angle=-555.5555... (-5deg @ ~111.11 steps/deg).
    controller.get_position.return_value = Position(x=2000.0, y=4000.0, angle=-555.5555555555555)

    sm = SystemStateMachine(controller)
    sm._homed = True
    sm._state = SystemState.IDLE

    position = sm.get_position()

    check("controller.get_position() (raw layer) was called", controller.get_position.called)
    check("X steps (2000) convert to 5.0 cm", approx(position.x, 5.0))
    check("Y steps (4000) convert to 5.0 cm", approx(position.y, 5.0))
    check("Angle steps (-555.56) convert to -5.0 deg", approx(position.angle, -5.0, tol=1e-3))

    # move_relative() and safe_return_to_position() must read the
    # CONVERTED position (self.get_position()), never the raw
    # controller layer directly, or they'd treat steps as cm/deg.
    import inspect
    src = inspect.getsource(SystemStateMachine.move_relative)
    check(
        "move_relative() reads position via self.get_position() (converted), not the raw controller",
        "self.get_position()" in src and "self._controller.get_position()" not in src,
    )
    src2 = inspect.getsource(SystemStateMachine.safe_return_to_position)
    check(
        "safe_return_to_position() reads position via self.get_position() (converted), not the raw controller",
        "self.get_position()" in src2 and "self._controller.get_position()" not in src2,
    )

    if _failures:
        print(f"\n{_failures} check(s) FAILED.")
        raise SystemExit(1)
    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
