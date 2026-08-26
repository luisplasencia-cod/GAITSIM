"""
Manual/offline verification for the 2026-08-26 calibration protocol
changes (see docs/protocol.md, Calibration Events):

  1. LIM{AXIS}MAX reports a raw motor step count instead of a value
     already converted to cm/deg by the ESP32, and CAL_PROGRESS was
     removed entirely ("Cambio 2026-08-26").
  2. The angular axis is special-cased ("Cambio 2026-08-26 (eje
     angular)"): LIMANGMIN now ALSO carries a signed step count (unlike
     Y/X's argument-less MIN), because the ESP32 itself reports steps
     already relative to horizontal = step 0 instead of the Raspberry
     Pi applying a fixed offset (ANGLE_HORIZONTAL_OFFSET_DEG, now
     removed) afterward.

Checks that SystemStateMachine converts raw steps into the
CalibrationSpace's cm/deg using the real rig constants
(STEPS_PER_CM_Y/STEPS_PER_CM_X/STEPS_PER_DEG_ANGLE), that Y/X min stays
0 while angular min can be negative, and that CAL_PROGRESS /
ANGLE_HORIZONTAL_OFFSET_DEG are both gone from the code.

Uses a mocked ESP32Controller — no real hardware/serial connection
needed — same spirit as the mocked-controller verification already
used elsewhere in this project's history.

Usage:
    python3 -m tests.manual_test_calibration_steps
"""

from unittest.mock import MagicMock

from src.communication import protocol
from src.communication.esp32_controller import ESP32Controller
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
    sm = SystemStateMachine(controller)
    sm._homed = False
    sm._state = SystemState.HOMING

    # Real physical step values supplied by the teammate for the
    # definitive firmware's rig (see docs/protocol.md): Y up to 72000
    # steps, X up to 48000 steps, Angular from -5000 (lower limit
    # switch) to 5400 (upper limit switch), signed relative to
    # horizontal = step 0.
    sm._on_calibration_limit("Y", "MIN", None)
    sm._on_calibration_limit("Y", "MAX", 72000.0)
    sm._on_calibration_limit("X", "MIN", None)
    sm._on_calibration_limit("X", "MAX", 48000.0)
    sm._on_calibration_limit("A", "MIN", -5000.0)
    sm._on_calibration_limit("A", "MAX", 5400.0)

    space = sm._build_calibration_space()

    check("Y range converts 72000 steps -> 90.0 cm", approx(space.y_range, 90.0))
    check("X range converts 48000 steps -> 120.0 cm", approx(space.x_range, 120.0))
    check(
        "Angle min converts -5000 steps -> -45.0 deg "
        "(matches teammate's HOMING_ZERO_MIN_K_DEG, no RPi-side offset)",
        approx(space.angle_min, -45.0, tol=1e-3),
    )
    check(
        "Angle max converts 5400 steps -> 48.6 deg "
        "(matches teammate's HOMING_ZERO_MAX_K_DEG, no RPi-side offset)",
        approx(space.angle_max, 48.6, tol=1e-3),
    )
    check(
        "Angular range is 93.6 deg",
        approx(space.angle_range, 93.6, tol=1e-3),
    )
    check("Y min stays 0.0 (raw wire zero IS the axis zero)", space.y_min == 0.0)
    check("X min stays 0.0 (raw wire zero IS the axis zero)", space.x_min == 0.0)
    check(
        "ANGLE_HORIZONTAL_OFFSET_DEG was removed from SystemStateMachine",
        not hasattr(sm, "ANGLE_HORIZONTAL_OFFSET_DEG"),
    )

    # CAL_PROGRESS must no longer exist on the wire parser at all.
    parsed = protocol.parse_response("<CAL_PROGRESS:Y:15.0000>")
    check("CAL_PROGRESS is no longer parsed (falls back to UNKNOWN)", parsed.kind == "UNKNOWN")
    check(
        "parse_calibration_progress() was removed from protocol.py",
        not hasattr(protocol, "parse_calibration_progress"),
    )
    check(
        "SystemStateMachine no longer exposes on_calibration_progress",
        not hasattr(sm, "on_calibration_progress"),
    )

    # LIM{AXIS}MAX still parses fine as a plain integer-valued payload.
    parsed_max = protocol.parse_response("<LIMYMAX:72000>")
    check("LIMYMAX still parses correctly", parsed_max.kind == "LIMYMAX" and parsed_max.payload == "72000")

    # LIMANGMIN now carries a signed argument too (unlike LIMYMIN/LIMXMIN).
    parsed_angmin = protocol.parse_response("<LIMANGMIN:-5000>")
    check(
        "LIMANGMIN now parses WITH a payload (unlike LIMYMIN/LIMXMIN)",
        parsed_angmin.kind == "LIMANGMIN" and parsed_angmin.payload == "-5000",
    )
    parsed_ymin = protocol.parse_response("<LIMYMIN>")
    check(
        "LIMYMIN still parses with no payload",
        parsed_ymin.kind == "LIMYMIN" and parsed_ymin.payload is None,
    )

    if _failures:
        print(f"\n{_failures} check(s) FAILED.")
        raise SystemExit(1)
    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
