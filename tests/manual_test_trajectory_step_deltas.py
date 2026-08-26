"""
Manual/offline verification for the 2026-08-26 trajectory-in-steps
change (see docs/protocol.md, Comandos de Transferencia de Trayectoria,
"Cambio 2026-08-26 (trayectorias en pasos)"): TRAJ_POINT now carries a
signed step DELTA per axis (relative to the previous point, or to the
platform's actual current position for the first point) instead of an
absolute cm/deg position. SystemStateMachine._points_to_step_deltas()
is the one place that conversion happens, for every trajectory sent
through send_trajectory() (CSV ensayos, joystick moves, safe return,
initial synchronized move alike).

Checks:
  - Deltas sum back to exactly the intended absolute step positions
    (no drift from independently-rounded per-step deltas).
  - The first delta is computed against the ACTUAL current position
    (self.get_position()), not assumed to already equal points[0].
  - len(deltas) == len(points) (n_points in TRAJ_BEGIN is unaffected).
  - _on_progress() converts incoming raw-step TRAJ_PROGRESS back to
    cm/deg before forwarding to the UI callback.

Uses a mocked ESP32Controller — no real hardware/serial connection
needed — same spirit as the mocked-controller verification already
used elsewhere in this project's history.

Usage:
    python3 -m tests.manual_test_trajectory_step_deltas
"""

from unittest.mock import MagicMock

from src.communication.esp32_controller import ESP32Controller
from src.communication.protocol import Position, TrajectoryPoint
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


def make_sm(current_position_steps: Position):
    controller = MagicMock(spec=ESP32Controller)
    controller.is_connected = True
    controller.get_position.return_value = current_position_steps
    sm = SystemStateMachine(controller)
    sm._homed = True
    sm._state = SystemState.IDLE
    return sm


def main():
    # --- Case 1: trajectory starts exactly at current position -> ---
    # --- first delta should be ~0, deltas should sum to the total. ---
    sm = make_sm(Position(x=0.0, y=0.0, angle=0.0))  # steps, = 0cm/0cm/0deg
    points = [
        TrajectoryPoint(t=0.0, x=0.0, y=0.0, angle=0.0),
        TrajectoryPoint(t=0.5, x=2.5, y=0.0, angle=0.0),
        TrajectoryPoint(t=1.0, x=5.0, y=0.0, angle=0.0),
    ]
    deltas = sm._points_to_step_deltas(points)

    check("len(deltas) == len(points)", len(deltas) == len(points))
    check("first delta is ~0 (trajectory starts at current position)", deltas[0].dx_steps == 0)
    total_dx = sum(d.dx_steps for d in deltas)
    check(
        f"deltas sum to exactly 5cm in steps (5.0 * STEPS_PER_CM_X = {5.0 * sm.STEPS_PER_CM_X:g})",
        total_dx == round(5.0 * sm.STEPS_PER_CM_X),
    )
    total_dt = sum(d.dt_ms for d in deltas)
    check("dt_ms sums to exactly 1000ms (1.0s total)", total_dt == 1000)

    # --- Case 2: NO independent-rounding drift over many small steps ---
    # 37 points, 0.1cm apart -> each individual cm-per-point would round
    # unevenly at 400 steps/cm (40 steps/point, no fractional issue here
    # by design) -- use an irrational-ish step to actually stress rounding.
    sm2 = make_sm(Position(x=0.0, y=0.0, angle=0.0))
    n = 37
    step_cm = 1.0 / 3.0  # deliberately not a clean multiple of 1/STEPS_PER_CM_X
    points2 = [TrajectoryPoint(t=i * 0.1, x=i * step_cm, y=0.0, angle=0.0) for i in range(n)]
    deltas2 = sm2._points_to_step_deltas(points2)
    total_dx2 = sum(d.dx_steps for d in deltas2)
    expected_total = round((n - 1) * step_cm * sm2.STEPS_PER_CM_X)
    check(
        f"no cumulative drift over {n} points with a non-exact step size "
        f"(total={total_dx2}, expected={expected_total})",
        total_dx2 == expected_total,
    )

    # --- Case 3: trajectory does NOT start at current position -> ---
    # --- first delta must reflect the real gap, not be dropped/zeroed. ---
    sm3 = make_sm(Position(x=2000.0, y=0.0, angle=0.0))  # 5cm in X already
    points3 = [
        TrajectoryPoint(t=0.0, x=10.0, y=0.0, angle=0.0),  # 10cm, not 5cm
        TrajectoryPoint(t=0.5, x=15.0, y=0.0, angle=0.0),
    ]
    deltas3 = sm3._points_to_step_deltas(points3)
    check(
        "first delta reflects the REAL gap from current position (5cm -> 10cm = +5cm in steps), not assumed 0",
        deltas3[0].dx_steps == round(5.0 * sm3.STEPS_PER_CM_X),
    )

    # --- Case 4: _on_progress converts raw steps back to cm/deg ---
    controller = MagicMock(spec=ESP32Controller)
    controller.is_connected = True
    sm4 = SystemStateMachine(controller)
    received = []
    sm4.on_trajectory_progress = lambda p: received.append(p)
    sm4._on_progress(TrajectoryPoint(t=1.5, x=2000.0, y=4000.0, angle=-555.5555555555555))
    check("_on_progress converted", len(received) == 1)
    p = received[0]
    check("_on_progress: t passed through unchanged", approx(p.t, 1.5))
    check("_on_progress: x steps -> cm", approx(p.x, 5.0))
    check("_on_progress: y steps -> cm", approx(p.y, 5.0))
    check("_on_progress: angle steps -> deg", approx(p.angle, -5.0, tol=1e-3))

    if _failures:
        print(f"\n{_failures} check(s) FAILED.")
        raise SystemExit(1)
    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
