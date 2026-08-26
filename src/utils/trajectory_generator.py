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

Two entry points:
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
"""

import math
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

# Sampling interval (seconds) between generated waypoints — independent
# of TRAJ_PROGRESS's own test-only 120ms reporting cadence (see
# docs/protocol.md); this just controls how finely the straight line in
# configuration space is diced into TRAJ_POINTs.
WAYPOINT_INTERVAL_S = 0.1

# Below this total distance (across all 3 axes combined), the move is
# treated as a no-op rather than generating a degenerate near-zero-
# duration trajectory.
_MIN_MOVE_DISTANCE = 1e-6


def _interpolate(start: Position, target: Position) -> List[TrajectoryPoint]:
    """
    Core synchronized straight-line interpolation from `start` to
    `target`, with NO calibration-space validation — callers are
    responsible for validating whatever of `start`/`target` is operator
    input before calling this (see generate_synchronized_trajectory,
    which validates `target`; generate_safe_return_trajectory, which
    validates `target` once and derives its own intermediate waypoints
    from already-real/already-validated positions).

    Returns a list of TrajectoryPoint starting at t=0 (start.x, start.y,
    start.angle) and ending at t=T (target.x, target.y, target.angle),
    sampled every WAYPOINT_INTERVAL_S. Empty list if target is
    indistinguishable from start (nothing to move).
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

    n_intervals = max(1, math.ceil(total_duration / WAYPOINT_INTERVAL_S))

    points = []
    for i in range(n_intervals + 1):
        t = min(i * WAYPOINT_INTERVAL_S, total_duration)
        fraction = t / total_duration if total_duration > 0 else 1.0
        points.append(
            TrajectoryPoint(
                t=t,
                x=start.x + delta_x * fraction,
                y=start.y + delta_y * fraction,
                angle=start.angle + delta_angle * fraction,
            )
        )
    # Guarantee the last point lands exactly on the target (floating-
    # point fraction accumulation could otherwise leave it a hair off).
    points[-1] = TrajectoryPoint(
        t=total_duration, x=target.x, y=target.y, angle=target.angle
    )
    return points


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
        A list of TrajectoryPoint, starting at t=0 (start.x, start.y,
        start.angle) and ending at t=T (target.x, target.y,
        target.angle), sampled every WAYPOINT_INTERVAL_S. Empty list if
        target is indistinguishable from start (nothing to move).
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
