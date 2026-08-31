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
  - A leading point at t=0.0 (the universal CSV/generated-trajectory
    convention) is dropped rather than sent as a wire TRAJ_POINT, since
    it would always carry dt_ms=0 — which the definitive ESP32 firmware
    treats as a hard failure (see run_trajectory() in the teammate's
    main.cpp). len(deltas) == len(points) - 1 in that case.
  - The first SENT delta is still computed against the ACTUAL current
    position (self.get_position()), not assumed to already equal
    points[0] — a real mismatch between the trajectory's declared start
    and reality is folded into that first sent delta rather than lost.
  - _on_progress() converts incoming raw-step TRAJ_PROGRESS back to
    cm/deg before forwarding to the UI callback.
  - timed=False (2026-08-31, "TRAJ_POINT sin tiempo") sets dt_ms=None
    on every delta, and build_trajectory_step_point() emits the
    3-field wire form (no dt_ms) for it instead of the usual 4-field
    form.

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

    check(
        "leading t=0 point dropped: len(deltas) == len(points) - 1",
        len(deltas) == len(points) - 1,
    )
    check(
        "first SENT delta reaches the t=0.5 point (2.5cm), not the dropped t=0 one",
        deltas[0].dx_steps == round(2.5 * sm.STEPS_PER_CM_X),
    )
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

    # --- Case 3: trajectory's declared start (t=0, dropped) does NOT ---
    # --- match current position -> the mismatch must still be folded ---
    # --- into the first SENT delta, not silently lost. ---
    sm3 = make_sm(Position(x=2000.0, y=0.0, angle=0.0))  # 5cm in X already
    points3 = [
        TrajectoryPoint(t=0.0, x=10.0, y=0.0, angle=0.0),  # declared start, 10cm (dropped)
        TrajectoryPoint(t=0.5, x=15.0, y=0.0, angle=0.0),
    ]
    deltas3 = sm3._points_to_step_deltas(points3)
    check(
        "single sent delta covers the REAL gap from current (5cm -> 15cm = +10cm in steps)",
        deltas3[0].dx_steps == round(10.0 * sm3.STEPS_PER_CM_X),
    )

    # --- Case 5 (2026-08-31, TRAJ_POINT sin tiempo): timed=False sets ---
    # --- dt_ms=None on every delta, and build_trajectory_step_point() ---
    # --- emits the 3-field wire form for it (no dt_ms at all). ---
    from src.communication import protocol

    sm5 = make_sm(Position(x=0.0, y=0.0, angle=0.0))
    points5 = [
        TrajectoryPoint(t=0.0, x=0.0, y=0.0, angle=0.0),
        TrajectoryPoint(t=0.5, x=2.0, y=0.0, angle=0.0),
    ]
    deltas5 = sm5._points_to_step_deltas(points5, timed=False)
    check("timed=False: dt_ms is None on the sent delta", deltas5[0].dt_ms is None)
    check(
        "timed=False: dx_steps still correct (2cm)",
        deltas5[0].dx_steps == round(2.0 * sm5.STEPS_PER_CM_X),
    )
    wire = protocol.build_trajectory_step_point(deltas5[0])
    check(
        f"timed=False: wire form has 3 fields, no dt_ms (got '{wire}')",
        wire == f"<TRAJ_POINT:{deltas5[0].dx_steps}:0:0>",
    )

    deltas5_timed = sm5._points_to_step_deltas(points5, timed=True)
    wire_timed = protocol.build_trajectory_step_point(deltas5_timed[0])
    check(
        f"timed=True (default): wire form still has 4 fields (got '{wire_timed}')",
        wire_timed == f"<TRAJ_POINT:500:{deltas5_timed[0].dx_steps}:0:0>",
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
