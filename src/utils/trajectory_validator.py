"""
trajectory_validator.py

Single source of truth for checking a position (or every point of a
full trajectory) against the calibrated movement space (CalibrationSpace,
see src/controllers/system_state.py). Every flow that can send a
movement or trajectory to the ESP32 — manual moves, the synchronized
initial/return trajectories, and CSV-loaded trials ("ensayos") — must
go through this before anything is transmitted, so out-of-range targets
are rejected consistently and with the same kind of actionable feedback
everywhere (which axis, by how much, and what value would be valid).

trajectory_generator.py uses this for its single-target checks (the
generated trajectory is a straight line in configuration space between
an already-valid start and the validated target, so checking the
target alone is sufficient there). CSV-loaded trajectories have no such
guarantee — arbitrary points from a file — so they must be checked
point-by-point via validate_trajectory().
"""

from dataclasses import dataclass
from typing import List, Optional, Tuple

from src.communication.protocol import Position, TrajectoryPoint
from src.controllers.system_state import CalibrationSpace


@dataclass
class AxisViolation:
    """One axis of one point falling outside the calibrated range."""
    axis_label: str    # human-readable, e.g. "X", "Y", "Ángulo"
    unit: str           # "cm" or "°"
    value: float
    min_limit: float
    max_limit: float

    @property
    def exceeds_max(self) -> bool:
        return self.value > self.max_limit

    @property
    def excess(self) -> float:
        return (
            self.value - self.max_limit
            if self.exceeds_max
            else self.min_limit - self.value
        )

    def describe(self) -> str:
        if self.exceeds_max:
            return (
                f"{self.axis_label} excede el máximo por "
                f"{self.excess:.1f}{self.unit}; ajusta el valor a "
                f"<= {self.max_limit:.1f}{self.unit} para quedar dentro del rango"
            )
        return (
            f"{self.axis_label} excede el mínimo por "
            f"{self.excess:.1f}{self.unit}; ajusta el valor a "
            f">= {self.min_limit:.1f}{self.unit} para quedar dentro del rango"
        )


class PositionOutOfRangeError(Exception):
    """A single position falls outside the calibrated movement space
    on one or more axes. Used for manual moves and the generated
    initial/return trajectories, where only the target needs checking."""

    def __init__(self, violations: List[AxisViolation]):
        self.violations = violations
        message = "Posición fuera del espacio calibrado: " + "; ".join(
            v.describe() for v in violations
        )
        super().__init__(message)


class TrajectoryOutOfRangeError(Exception):
    """One or more points of a full trajectory fall outside the
    calibrated movement space. Used for CSV-loaded trials, where every
    point (not just the endpoints) must be checked individually."""

    # Cap how many offending points are listed in the message so a
    # badly-out-of-range file (potentially thousands of points) doesn't
    # produce an unreadable wall of text.
    MAX_LISTED = 5

    def __init__(self, point_violations: List[Tuple[int, float, List[AxisViolation]]]):
        self.point_violations = point_violations
        lines = []
        for index, t, violations in point_violations[: self.MAX_LISTED]:
            joined = "; ".join(v.describe() for v in violations)
            lines.append(f"Punto {index} (t={t:g}s): {joined}")
        remaining = len(point_violations) - self.MAX_LISTED
        if remaining > 0:
            lines.append(f"... y {remaining} punto(s) más fuera de rango")
        message = (
            f"{len(point_violations)} punto(s) de la trayectoria fuera del "
            f"espacio calibrado:\n" + "\n".join(lines)
        )
        super().__init__(message)


def check_position(position: Position, space: CalibrationSpace) -> List[AxisViolation]:
    """Return every axis of `position` that falls outside `space`
    (empty list if it's fully within range)."""
    violations = []
    if not (space.x_min <= position.x <= space.x_max):
        violations.append(
            AxisViolation("X", "cm", position.x, space.x_min, space.x_max)
        )
    if not (space.y_min <= position.y <= space.y_max):
        violations.append(
            AxisViolation("Y", "cm", position.y, space.y_min, space.y_max)
        )
    if not (space.angle_min <= position.angle <= space.angle_max):
        violations.append(
            AxisViolation(
                "Ángulo", "°", position.angle, space.angle_min, space.angle_max
            )
        )
    return violations


def validate_position(position: Position, space: CalibrationSpace) -> None:
    """Raise PositionOutOfRangeError if `position` falls outside `space`
    on any axis."""
    violations = check_position(position, space)
    if violations:
        raise PositionOutOfRangeError(violations)


def validate_trajectory(
    points: List[TrajectoryPoint], space: CalibrationSpace
) -> None:
    """
    Raise TrajectoryOutOfRangeError if ANY point of `points` falls
    outside `space` on any axis — unlike validate_position, every point
    is checked individually rather than just the endpoints, since a
    CSV-loaded trajectory has no guarantee of being a straight line
    between two already-valid positions.

    Call this on the FINAL absolute points (after any initial-position
    offset has been applied) — i.e. exactly what would actually be sent
    to the ESP32 — not the raw file-relative points.
    """
    point_violations = []
    for index, point in enumerate(points):
        violations = check_position(
            Position(x=point.x, y=point.y, angle=point.angle), space
        )
        if violations:
            point_violations.append((index, point.t, violations))
    if point_violations:
        raise TrajectoryOutOfRangeError(point_violations)
