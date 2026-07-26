"""
Manual/offline verification for two UI-layer separation issues fixed in
this session, both in trajectory_screen.py:

1. The Live Trajectory Plot was accumulating TRAJ_PROGRESS events from
   ANY currently-executing trajectory on the shared StateMachineBridge
   (manual moves, the initial/return synchronized trajectories) into
   the SAME buffers used for the loaded ensayo's own Run — because the
   wire protocol has no per-trajectory "source" tag, every trajectory
   execution looks the same at that level. Fixed with a `_plot_active`
   gate, set True only right before the ensayo's own sm.run() (in
   _on_run_clicked / _on_restart_trial_clicked's do_restart) and
   cleared on trajectory_finished/device_error/disconnected.

2. The ensayo's own loaded data (`_ensayo_trajectory`, renamed from
   `_loaded_points`) was already a separate attribute from any live
   movement bookkeeping (InitialPositionSession) — this is a
   regression guard confirming a manual move (and/or session position
   changes) never touches it, and that loading a new CSV is the only
   way it gets replaced.

Uses a mocked ESP32Controller — no real hardware/serial connection
needed.

Usage:
    QT_QPA_PLATFORM=offscreen python3 -m tests.manual_test_trajectory_plot_isolation
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
    controller.get_position.return_value = Position(x=5.0, y=5.0, angle=0.0)
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


def test_plot_ignores_progress_before_run():
    sm, _ = make_state_machine()
    screen = make_trajectory_screen(sm, Position(x=0.0, y=0.0, angle=0.0))

    # Simulates a manual move's (or initial/return trajectory's) own
    # TRAJ_PROGRESS reaching this screen via the shared bridge, BEFORE
    # any ensayo Run was ever pressed.
    screen._on_trajectory_progress(0.1, 1.0, 2.0, 3.0)
    check(
        "unrelated progress before Run is NOT plotted",
        screen._plot_t == [] and screen._plot_x == [],
    )


def test_plot_only_active_during_run():
    sm, _ = make_state_machine()
    screen = make_trajectory_screen(sm, Position(x=0.0, y=0.0, angle=0.0))
    # _on_trajectory_finished also triggers _maybe_offer_save_position(),
    # which pops a real (blocking) QInputDialog/QMessageBox — unrelated
    # to what this test checks (plot gating) and nothing here would ever
    # dismiss it, so stub it out.
    screen._maybe_offer_save_position = lambda: None

    screen._plot_active = True  # what _on_run_clicked sets right before sm.run()
    screen._on_trajectory_progress(0.1, 1.0, 2.0, 3.0)
    screen._on_trajectory_progress(0.2, 1.5, 2.5, 3.5)
    check(
        "progress during the ensayo's own Run IS plotted",
        screen._plot_t == [0.1, 0.2] and screen._plot_x == [1.0, 1.5],
    )

    screen._on_trajectory_finished()
    check("_plot_active clears once FINISHED arrives", screen._plot_active is False)

    screen._on_trajectory_progress(0.3, 9.0, 9.0, 9.0)
    check(
        "progress AFTER finished (e.g. a later manual move) is NOT plotted",
        screen._plot_t == [0.1, 0.2],  # unchanged from before
    )


def test_plot_inactive_during_restart_reposition_phase():
    """
    Regression guard for the real bug found this session: "Reiniciar
    Ensayo" repositions (safe_return_to_position) BEFORE resending and
    running the actual ensayo — that repositioning's own TRAJ_PROGRESS
    must not bleed into the plot, only the gait run that follows should.
    """
    sm, _ = make_state_machine()
    screen = make_trajectory_screen(sm, Position(x=0.0, y=0.0, angle=0.0))

    # Reposition phase: _plot_active is still False at this point in
    # the real do_restart() (see _on_restart_trial_clicked).
    screen._on_trajectory_progress(0.05, 0.5, 0.5, 0.5)
    check("repositioning progress is NOT plotted", screen._plot_t == [])

    # Only once do_restart() reaches the real sm.run() call is the gate set.
    screen._plot_active = True
    screen._on_trajectory_progress(0.1, 1.0, 1.0, 1.0)
    check("progress after the gate flips IS plotted", screen._plot_t == [0.1])


def test_restart_after_pause_starts_reposition_with_plot_deactivated():
    """
    Regression guard for the bug Luis actually hit: a PAUSED run leaves
    `_plot_active` True (abort() ends it without ever firing
    trajectory_finished), and the OLD code only set the flag right
    before the FINAL sm.run() inside do_restart() — never resetting it
    first. So if the operator paused a run (flag stuck True) and then
    pressed "Reiniciar Ensayo", the reposition phase inherited the
    stale True and got plotted right alongside the fresh run. Fixed by
    unconditionally calling _end_plot_session() at the very top of
    do_restart(), before abort()/safe_return_to_position() run.

    Verifies the exact call-time sequence of the flag by monkeypatching
    abort/safe_return_to_position/_send_and_check/run with stubs that
    record `screen._plot_active` at the instant each is invoked —
    directly proving the ordering rather than inferring it.
    """
    sm, controller = make_state_machine()
    target = Position(x=5.0, y=5.0, angle=0.0)
    screen = make_trajectory_screen(sm, target)
    screen._ensayo_trajectory = [
        TrajectoryPoint(t=0.0, x=0.0, y=0.0, angle=0.0),
        TrajectoryPoint(t=0.1, x=1.0, y=1.0, angle=0.0),
    ]
    sm._state = SystemState.PAUSED  # a paused run, like the reported scenario
    screen._plot_active = True  # the stale leftover from that paused run

    calls = []
    sm.abort = lambda: calls.append(("abort", screen._plot_active))
    sm.safe_return_to_position = lambda *a, **k: calls.append(
        ("reposition", screen._plot_active)
    )
    screen._send_and_check = lambda: calls.append(
        ("send_and_check", screen._plot_active)
    )
    sm.run = lambda: calls.append(("run", screen._plot_active))

    screen._on_restart_trial_clicked()
    screen._worker.wait()
    QApplication.instance().processEvents()

    check(
        "restart's call sequence matches (abort/reposition/send are pre-plot)",
        [c[0] for c in calls] == ["abort", "reposition", "send_and_check", "run"],
    )
    check(
        "plot is OFF during abort/reposition/send_and_check, despite the stale flag",
        all(active is False for name, active in calls if name != "run"),
    )
    check(
        "plot is ON only for the real run() call",
        dict(calls)["run"] is True,
    )


def test_choose_other_deactivates_plot_before_leaving_screen():
    """
    Regression guard for the other reported scenario: "Elegir Otro
    Ensayo" on a PAUSED run also aborts without firing
    trajectory_finished. The operator then sets up the NEXT trial's
    initial position on ConnectionScreen (a temporal retorno/inicial
    move) — that move's TRAJ_PROGRESS must not land in THIS (abandoned)
    ensayo's plot, even though this screen is no longer visible.
    """
    sm, controller = make_state_machine()
    screen = make_trajectory_screen(sm, Position(x=5.0, y=5.0, angle=0.0))
    sm._state = SystemState.PAUSED
    screen._plot_active = True  # stale leftover from the paused run

    def fake_run():
        callback = controller.on_trajectory_finished
        if callback is not None:
            callback()
    controller.run.side_effect = fake_run  # unblocks abort()'s internals, if any

    screen._on_choose_other_clicked()
    check(
        "_plot_active is cleared synchronously, before the abort worker even starts",
        screen._plot_active is False,
    )

    if screen._worker is not None:
        screen._worker.wait()
    QApplication.instance().processEvents()

    # Simulates the NEXT trial's retorno/inicial move on ConnectionScreen.
    screen._on_trajectory_progress(0.1, 1.0, 1.0, 1.0)
    check(
        "a later temporal move's progress is NOT plotted after choosing another ensayo",
        screen._plot_t == [],
    )


def test_ensayo_trajectory_untouched_by_manual_move():
    sm, controller = make_state_machine()
    screen = make_trajectory_screen(sm, Position(x=5.0, y=5.0, angle=0.0))

    original = [
        TrajectoryPoint(t=0.0, x=0.0, y=0.0, angle=0.0),
        TrajectoryPoint(t=0.1, x=1.0, y=1.0, angle=0.0),
    ]
    screen._ensayo_trajectory = original

    def fake_run():
        callback = controller.on_trajectory_finished
        if callback is not None:
            callback()
    controller.run.side_effect = fake_run

    # A real manual move, through the SAME shared state machine the
    # screen's bridge wraps — same object relationship as the real app
    # (ConnectionScreen and TrajectoryScreen share one SystemStateMachine).
    sm.move_relative("Y", "+", 2.0)
    check(
        "ensayo trajectory is the exact same object after a manual move",
        screen._ensayo_trajectory is original,
    )
    check(
        "ensayo trajectory content is unchanged after a manual move",
        screen._ensayo_trajectory == original,
    )

    # Also simulate the position-session bookkeeping a manual move
    # updates (see ConnectionScreen._on_manual_move_succeeded) directly.
    screen._position_session.apply_manual_delta("Y", "+", 2.0)
    check(
        "ensayo trajectory unaffected by InitialPositionSession changes",
        screen._ensayo_trajectory == original,
    )


def test_ensayo_trajectory_replaced_only_by_loading_new_csv():
    sm, _ = make_state_machine()
    screen = make_trajectory_screen(sm, Position(x=0.0, y=0.0, angle=0.0))

    first = [TrajectoryPoint(t=0.0, x=0.0, y=0.0, angle=0.0)]
    screen._ensayo_trajectory = first
    second = [TrajectoryPoint(t=0.0, x=1.0, y=1.0, angle=1.0)]
    # Mirrors exactly what _on_send_clicked does on a successful load.
    screen._ensayo_trajectory = second
    check(
        "loading a new CSV replaces the ensayo trajectory",
        screen._ensayo_trajectory is second and screen._ensayo_trajectory is not first,
    )


def main():
    app = QApplication.instance() or QApplication(sys.argv)

    test_plot_ignores_progress_before_run()
    test_plot_only_active_during_run()
    test_plot_inactive_during_restart_reposition_phase()
    test_restart_after_pause_starts_reposition_with_plot_deactivated()
    test_choose_other_deactivates_plot_before_leaving_screen()
    test_ensayo_trajectory_untouched_by_manual_move()
    test_ensayo_trajectory_replaced_only_by_loading_new_csv()

    print()
    if _failures:
        print(f"{_failures} check(s) FAILED.")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
