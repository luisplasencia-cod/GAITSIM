"""
Manual/offline verification for the 2026-08-31 calibration protocol
change (see docs/protocol.md, Calibración, "Cambio 2026-08-31 (READY
con límites)"):

  HOME's response collapsed from 6 live LIM{AXIS}MIN/MAX events (2026-
  08-26 wire format) + a bare READY into a single
  "READY:xmax:ymax:amin:amax" line — no more intermediate events, no
  more '<' '>' framing exception for calibration events.

Checks that protocol.parse_response()/parse_home_limits() handle the
new READY payload, that SystemStateMachine._build_calibration_space()
converts the resulting HomeLimits into cm/deg using the real rig
constants (STEPS_PER_CM_Y/STEPS_PER_CM_X/STEPS_PER_DEG_ANGLE) with Y/X
min staying 0 while angular min can be negative, and that the old
per-event machinery (LIM* constants, CALIBRATION_LIMIT_KINDS,
on_calibration_limit) is gone from the code.

Uses a mocked ESP32Controller — no real hardware/serial connection
needed — same spirit as the mocked-controller verification already
used elsewhere in this project's history.

Usage:
    python3 -m tests.manual_test_calibration_steps
"""

from unittest.mock import MagicMock

from src.communication import protocol
from src.communication.esp32_controller import ESP32Controller
from src.controllers.system_state import SystemStateMachine

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

    # Real physical step values supplied by the teammate for the
    # definitive firmware's rig (see docs/protocol.md): Y up to 72000
    # steps, X up to 48000 steps, Angular from -5000 (lower limit
    # switch) to 5400 (upper limit switch), signed relative to
    # horizontal = step 0.
    parsed = protocol.parse_response("READY:48000:72000:-5000:5400")
    check("READY parses with kind='READY'", parsed.kind == "READY")
    check(
        "READY payload is the raw 'xmax:ymax:amin:amax' text",
        parsed.payload == "48000:72000:-5000:5400",
    )

    limits = protocol.parse_home_limits(parsed.payload)
    check("parse_home_limits reads x_max=48000", limits.x_max == 48000.0)
    check("parse_home_limits reads y_max=72000", limits.y_max == 72000.0)
    check("parse_home_limits reads angle_min=-5000", limits.angle_min == -5000.0)
    check("parse_home_limits reads angle_max=5400", limits.angle_max == 5400.0)

    space = sm._build_calibration_space(limits)

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

    # The old per-event LIM* machinery must be gone entirely — a stray
    # "<LIM...>" line from an out-of-date firmware should now fall back
    # to UNKNOWN, same as any other unrecognized line, instead of being
    # specially unwrapped.
    parsed_lim = protocol.parse_response("<LIMYMAX:72000>")
    check(
        "Old-style <LIM...> events are no longer recognized (fall back to UNKNOWN)",
        parsed_lim.kind == "UNKNOWN",
    )
    check(
        "CALIBRATION_LIMIT_KINDS was removed from protocol.py",
        not hasattr(protocol, "CALIBRATION_LIMIT_KINDS"),
    )
    check(
        "SystemStateMachine no longer exposes on_calibration_limit",
        not hasattr(sm, "on_calibration_limit"),
    )

    if _failures:
        print(f"\n{_failures} check(s) FAILED.")
        raise SystemExit(1)
    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
