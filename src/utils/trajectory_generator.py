"""
trajectory_generator.py

Generates a synchronized ("optimal") multi-axis trajectory from the
HOME origin (0, 0, 0) to a target initial position, as an alternative
to an instantaneous GOTO jump. "Synchronized" means all 3 axes (X, Y,
angle) start and finish at exactly the same time: the axis with the
largest travel time (distance / its own max speed) sets the total
duration, and the other axes are sampled along their own straight line
from 0 to their target value over that same duration — a straight line
in configuration space, not a per-axis independent move.

Output is a plain List[TrajectoryPoint], the same type produced by
trajectory_loader.py from a CSV — the result is meant to be sent and
run through the existing TRAJ_BEGIN/TRAJ_POINT/TRAJ_END + RUN protocol
(see docs/protocol.md), which is already generic over where the points
came from. No new wire protocol or firmware changes are needed for
this.
"""

import math
from typing import List

from src.communication.protocol import Position, TrajectoryPoint
from src.controllers.system_state import CalibrationSpace

# Assumed max speed per axis (cm/s for X/Y, deg/s for angle), used only
# to compute how long each axis WOULD take on its own so the slowest
# axis can set the shared total duration. Placeholders until the real
# rig's motor speeds are known — same spirit as
# SystemStateMachine.Y_LIFT_MARGIN_CM / ANGLE_HORIZONTAL_OFFSET_DEG,
# not tied to any real mechanical measurement yet.
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


class TrajectoryGenerationError(Exception):
    """Base exception for all trajectory_generator failures."""
    pass


class PositionOutOfRangeError(TrajectoryGenerationError):
    """The requested target position falls outside the available
    movement space computed by the most recent HOME (see
    SystemStateMachine.CalibrationSpace)."""
    pass


def _validate_within_space(target: Position, space: CalibrationSpace) -> None:
    problems = []
    if not (space.x_min <= target.x <= space.x_max):
        problems.append(f"X={target.x:g} fuera de [{space.x_min:g}, {space.x_max:g}]")
    if not (space.y_min <= target.y <= space.y_max):
        problems.append(f"Y={target.y:g} fuera de [{space.y_min:g}, {space.y_max:g}]")
    if not (space.angle_min <= target.angle <= space.angle_max):
        problems.append(
            f"Ángulo={target.angle:g} fuera de "
            f"[{space.angle_min:g}, {space.angle_max:g}]"
        )
    if problems:
        raise PositionOutOfRangeError(
            "Posición inicial fuera del espacio calibrado: " + "; ".join(problems)
        )


def generate_synchronized_trajectory(
    target: Position, calibration_space: CalibrationSpace
) -> List[TrajectoryPoint]:
    """
    Build a synchronized straight-line trajectory from (0, 0, 0) — the
    HOME origin — to `target`, validated against `calibration_space`.

    Raises:
        PositionOutOfRangeError: If `target` falls outside the
            available movement space for any axis. No points are
            generated in that case.

    Returns:
        A list of TrajectoryPoint, starting at t=0 (x=0, y=0, angle=0)
        and ending at t=T (target.x, target.y, target.angle), sampled
        every WAYPOINT_INTERVAL_S. Empty list if target is
        indistinguishable from the origin (nothing to move).
    """
    _validate_within_space(target, calibration_space)

    if (
        abs(target.x) + abs(target.y) + abs(target.angle)
        < _MIN_MOVE_DISTANCE
    ):
        return []

    duration_x = abs(target.x) / SPEED_X_CM_S
    duration_y = abs(target.y) / SPEED_Y_CM_S
    duration_angle = abs(target.angle) / SPEED_ANGLE_DEG_S
    total_duration = max(duration_x, duration_y, duration_angle)

    n_intervals = max(1, math.ceil(total_duration / WAYPOINT_INTERVAL_S))

    points = []
    for i in range(n_intervals + 1):
        t = min(i * WAYPOINT_INTERVAL_S, total_duration)
        fraction = t / total_duration if total_duration > 0 else 1.0
        points.append(
            TrajectoryPoint(
                t=t,
                x=target.x * fraction,
                y=target.y * fraction,
                angle=target.angle * fraction,
            )
        )
    # Guarantee the last point lands exactly on the target (floating-
    # point fraction accumulation could otherwise leave it a hair off).
    points[-1] = TrajectoryPoint(
        t=total_duration, x=target.x, y=target.y, angle=target.angle
    )
    return points
