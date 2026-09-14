"""
trajectory_generator.py

Generates a synchronized ("optimal") multi-axis trajectory between two
(x, y, angle) positions, as an alternative to an instantaneous GOTO
jump. "Synchronized" means all 3 axes (X, Y, angle) start and finish at
exactly the same time: the axis with the largest travel time (distance
/ its own max speed) sets the total duration, and the other axes are
sampled along their own straight line from their start value to their
target value over that same duration — a straight line in
configuration space, not a per-axis independent move.

Output is a plain List[TrajectoryPoint], the same type produced by
trajectory_loader.py from a CSV — the result is meant to be sent and
run through the existing TRAJ_BEGIN/TRAJ_POINT/TRAJ_END + RUN protocol
(see docs/protocol.md), which is already generic over where the points
came from. No new wire protocol or firmware changes are needed for
this.

Three entry points:
    - generate_synchronized_trajectory(target, calibration_space,
      start=...): a single synchronized straight-line move, used as-is
      for the post-calibration (0, 0, 0) -> initial-position move.
    - generate_safe_return_trajectory(...): 5 such moves stitched into
      one continuous trajectory, used for repositioning FROM an
      arbitrary current position (restart trial / choose other trial)
      without ever letting Y drop below a safety floor while X/angle
      are still in transit, AND without ever moving X off the
      reference angle — see that function's docstring and
      SystemStateMachine.safe_return_to_position(), which is the only
      caller.
    - generate_detach_trajectory(...): 2 such moves stitched together,
      used right before Run when a tara (basal, no-contact reference)
      is on record for the loaded ensayo, so a force-platform contact
      test doesn't start the run already in contact — see that
      function's docstring and SystemStateMachine.perform_pre_run_detach(),
      which is the only caller.
"""

from typing import List

from src.communication.protocol import Position, TrajectoryPoint
from src.controllers.system_state import CalibrationSpace
from src.utils.trajectory_validator import PositionOutOfRangeError, validate_position

# Assumed max speed per axis (cm/s for X/Y, deg/s for angle), used only
# to compute how long each axis WOULD take on its own so the slowest
# axis can set the shared total duration. Placeholders until the real
# rig's motor speeds are known — same spirit as
# SystemStateMachine.Y_LIFT_MARGIN_CM, not tied to any real mechanical
# measurement yet.
SPEED_X_CM_S = 5.0
SPEED_Y_CM_S = 5.0
SPEED_ANGLE_DEG_S = 15.0

# Below this total distance (across all 3 axes combined), the move is
# treated as a no-op rather than generating a degenerate near-zero-
# duration trajectory.
_MIN_MOVE_DISTANCE = 1e-6


def _interpolate(start: Position, target: Position) -> List[TrajectoryPoint]:
    """
    Core synchronized straight-line move from `start` to `target`, with
    NO calibration-space validation — callers are responsible for
    validating whatever of `start`/`target` is operator input before
    calling this (see generate_synchronized_trajectory, which validates
    `target`; generate_safe_return_trajectory, which validates `target`
    once and derives its own intermediate waypoints from already-real/
    already-validated positions).

    Returns exactly 2 points — (start.x, start.y, start.angle) at t=0
    and (target.x, target.y, target.angle) at t=T — rather than
    subdividing the straight line into intermediate waypoints: the
    ESP32 firmware moves all 3 axes concurrently at their own constant
    velocity (delta/T) for the whole span of a single TRAJ_POINT (see
    run_trajectory() in the definitive firmware's main.cpp), so one
    point per straight segment already produces smooth synchronized
    motion — subdividing further only adds points without changing the
    physical motion, and risks exceeding the firmware's fixed-size
    trajectory buffer (MAX_DATA_LENGTH, configurable_motion_data.h) on
    long or multi-phase moves. The leading t=0 point exists so callers
    can validate/stitch against a known start; it never reaches the
    wire as a TRAJ_POINT (SystemStateMachine._points_to_step_deltas()
    drops any point at t=0, since the ESP32 rejects a zero-duration
    TRAJ_POINT). Empty list if target is indistinguishable from start
    (nothing to move).
    """
    delta_x = target.x - start.x
    delta_y = target.y - start.y
    delta_angle = target.angle - start.angle

    if abs(delta_x) + abs(delta_y) + abs(delta_angle) < _MIN_MOVE_DISTANCE:
        return []

    duration_x = abs(delta_x) / SPEED_X_CM_S
    duration_y = abs(delta_y) / SPEED_Y_CM_S
    duration_angle = abs(delta_angle) / SPEED_ANGLE_DEG_S
    total_duration = max(duration_x, duration_y, duration_angle)

    return [
        TrajectoryPoint(t=0.0, x=start.x, y=start.y, angle=start.angle),
        TrajectoryPoint(t=total_duration, x=target.x, y=target.y, angle=target.angle),
    ]


def generate_synchronized_trajectory(
    target: Position,
    calibration_space: CalibrationSpace,
    start: Position = Position(0.0, 0.0, 0.0),
) -> List[TrajectoryPoint]:
    """
    Build a synchronized straight-line trajectory from `start` to
    `target` (defaults to the HOME origin, (0, 0, 0), for the
    post-calibration initial-position move). Only `target` is
    validated against `calibration_space` — `start` is assumed to
    already be a real, reachable position (either the origin, or the
    system's actual current position as reported by GET_POSITION).

    Raises:
        PositionOutOfRangeError: If `target` falls outside the
            available movement space for any axis. No points are
            generated in that case.

    Returns:
        Exactly 2 points — t=0 (start.x, start.y, start.angle) and t=T
        (target.x, target.y, target.angle) — see _interpolate(). Empty
        list if target is indistinguishable from start (nothing to
        move).
    """
    validate_position(target, calibration_space)
    return _interpolate(start, target)


def _append_phase(
    points: List[TrajectoryPoint], phase: List[TrajectoryPoint], time_offset: float
) -> float:
    """
    Append `phase` (a trajectory starting at its own t=0) onto `points`,
    shifting its timestamps by `time_offset` so the combined trajectory
    has one continuous, strictly increasing time base. If `points`
    already ends exactly where `phase` begins (the common case when
    stitching adjacent safe-return phases), the duplicate boundary
    point is dropped rather than appended.

    Returns the time_offset the NEXT phase should use (this phase's own
    duration added on top).
    """
    if not phase:
        return time_offset
    start_index = 1 if points and phase[0].t == 0.0 else 0
    for point in phase[start_index:]:
        points.append(
            TrajectoryPoint(
                t=point.t + time_offset,
                x=point.x,
                y=point.y,
                angle=point.angle,
            )
        )
    return phase[-1].t + time_offset


def generate_safe_return_trajectory(
    current: Position,
    target: Position,
    floor_y: float,
    calibration_space: CalibrationSpace,
    lift_margin_cm: float,
    angle_reference_deg: float,
) -> List[TrajectoryPoint]:
    """
    Build a smooth repositioning trajectory from `current` (the
    system's actual current position, wherever that is) to `target` (a
    trial's initial position), WITHOUT ever letting Y drop below
    `floor_y` while X/angle are not yet at their target values, AND
    without ever moving X while angle is anywhere other than
    `angle_reference_deg` — both hard safety/mechanical rules inherited
    unchanged from the step-wise SystemStateMachine.safe_return_to_position()
    this replaces (see that method's docstring: a prosthesis may be
    mounted at `floor_y`'s height, and X travel is only mechanically
    safe at the reference angle). This is the ONLY sanctioned caller of
    this function — it exists to give that exact safety sequence
    smooth, visualized motion instead of a chain of instantaneous
    GOTOs, NOT to re-plan or shortcut it.

    Both invariants are preserved by keeping the EXACT SAME 5-step
    ordering as the original, just replacing each step's instantaneous
    jump with a synchronized (necessarily single-axis, since each step
    only ever moves one axis) sub-trajectory, stitched into one
    continuous timeline:

        1. Lift: `current` -> (current.x, lift_y, current.angle).
           Y only; X/angle unchanged.
        2. Rotate to reference: -> (current.x, lift_y, angle_reference_deg).
           Angle only, while still elevated.
        3. Move X: -> (target.x, lift_y, angle_reference_deg).
           X only, while still elevated and at the reference angle.
        4. Rotate to target: -> (target.x, lift_y, target.angle).
           Angle only, while still elevated.
        5. Descend: -> `target`. Y only; X/angle already match target,
           so this is the one step where Y may legitimately end up
           below floor_y.

    `lift_y` is computed the same way as the original step-wise
    version (`max(current.y, floor_y) + lift_margin_cm`), but capped at
    `calibration_space.y_max` — since this trajectory is fully
    generated up front (not one blocking GOTO at a time), there is no
    runtime rejection to catch and recover from mid-sequence the way
    the original could; capping proactively against the known
    calibration bounds achieves the same "don't ask for more lift than
    the rig can give" effect ahead of time instead.

    Raises:
        PositionOutOfRangeError: If `target` falls outside
            `calibration_space` on any axis. No points are generated in
            that case. `current` is NOT validated (it is the system's
            real, already-reached position).

    Returns:
        A single continuous List[TrajectoryPoint] spanning all 5
        steps, t starting at 0. Individual steps that are already
        no-ops (e.g. step 2 when current.angle already equals the
        reference) are omitted from the stitched result, but note the
        lift+descend steps still run even if `current == target`
        exactly (whenever `lift_margin_cm` > 0) — mirroring the
        original step-wise safe_return_to_position(), which never
        special-cased that either. Empty list only in the fully
        degenerate case where every step is simultaneously a no-op.
    """
    validate_position(target, calibration_space)

    lift_y = min(
        max(current.y, floor_y) + lift_margin_cm, calibration_space.y_max
    )

    step1_end = Position(current.x, lift_y, current.angle)
    step2_end = Position(current.x, lift_y, angle_reference_deg)
    step3_end = Position(target.x, lift_y, angle_reference_deg)
    step4_end = Position(target.x, lift_y, target.angle)

    points: List[TrajectoryPoint] = []
    offset = 0.0
    offset = _append_phase(points, _interpolate(current, step1_end), offset)
    offset = _append_phase(points, _interpolate(step1_end, step2_end), offset)
    offset = _append_phase(points, _interpolate(step2_end, step3_end), offset)
    offset = _append_phase(points, _interpolate(step3_end, step4_end), offset)
    offset = _append_phase(points, _interpolate(step4_end, target), offset)

    if not points:
        return []
    # Guarantee the very last point lands exactly on target, same
    # reasoning as generate_synchronized_trajectory's final snap.
    points[-1] = TrajectoryPoint(
        t=points[-1].t, x=target.x, y=target.y, angle=target.angle
    )
    return points


def generate_detach_trajectory(
    current: Position,
    tara_y: float,
    lift_above_tara_cm: float,
    x_shift_cm: float,
    calibration_space: CalibrationSpace,
) -> List[TrajectoryPoint]:
    """
    "Platform detach" trajectory run right before Run whenever a tara
    (basal, no-contact reference) has been recorded for the loaded
    ensayo — see SystemStateMachine.perform_pre_run_detach(), the only
    sanctioned caller, and the contact-threshold testing workflow it
    supports: an operator manually lowers Y in small increments from
    the tara reference to find the force platform's contact threshold,
    then presses Run at that (already touching) Y. Running straight
    from there would start the gait trajectory — and its force trace —
    with contact already established. This trajectory instead lifts
    clear of the platform, shifts sideways to fully detach, then swings
    back down into the EXACT same (x, y, angle) `current` position the
    operator was at, so the platform arrives back in contact already IN
    MOTION rather than starting cold — the actual gait run (sent/run
    separately, immediately after this) then begins smoothly from
    there.

    Two synchronized (multi-axis-at-once) legs, mirroring the "en el
    mismo tiempo" requirement Luis specified for the return leg:

        1. Lift + shift back: `current` -> (current.x - x_shift_cm,
           min(tara_y + lift_above_tara_cm, calibration_space.y_max),
           current.angle). Y and X move together, angle unchanged —
           unlike generate_safe_return_trajectory, this never touches
           angle, so there is no ANGLE_REFERENCE_DEG-style constraint
           forcing X and Y apart here.
        2. Shift forward + descend: -> `current`. Symmetric return.

    `lift_above_tara_cm`/`x_shift_cm` are Luis's fixed constants (see
    SystemStateMachine.DETACH_LIFT_ABOVE_TARA_CM/DETACH_X_SHIFT_CM).
    The lift target is capped at `calibration_space.y_max` (graceful
    cap, same pattern as generate_safe_return_trajectory's lift_y) and
    the shift target at `calibration_space.x_min` (always 0.0, X never
    goes negative) — defensive backstops only; in normal operation
    (per Luis) real trials stay well clear of X=0, so this cap is not
    expected to actually bind.

    `current` is NOT validated against `calibration_space` (same
    reasoning as generate_safe_return_trajectory: it is the system's
    real, already-reached position) — the intermediate detach point is
    implicitly kept in range by the clamping above, not by a separate
    validate_position() call.

    Returns:
        A single continuous List[TrajectoryPoint] spanning both legs, t
        starting at 0, ending exactly back at `current`. Empty list
        only in the degenerate case where both legs are simultaneously
        no-ops.
    """
    lift_y = min(tara_y + lift_above_tara_cm, calibration_space.y_max)
    shifted_x = max(current.x - x_shift_cm, calibration_space.x_min)
    detached = Position(shifted_x, lift_y, current.angle)

    points: List[TrajectoryPoint] = []
    offset = 0.0
    offset = _append_phase(points, _interpolate(current, detached), offset)
    offset = _append_phase(points, _interpolate(detached, current), offset)

    if not points:
        return []
    points[-1] = TrajectoryPoint(
        t=points[-1].t, x=current.x, y=current.y, angle=current.angle
    )
    return points
