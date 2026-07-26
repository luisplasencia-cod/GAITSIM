"""
limit_violation_dialog.py

Modal dialog shown whenever a movement/trajectory is rejected by the
calibration-space validation (src/utils/trajectory_validator.py) —
manual moves, the initial/return synchronized trajectories, and
CSV-loaded ensayos all funnel into this SAME dialog, only the short
context line changes. Replaces plain status-label text for this one
specific kind of failure with a dedicated visual warning: the map
already used for "Espacio Disponible" (CalibrationMapView, reused
as-is, not redrawn from scratch) with the exceeded axis/boundary and
offending point highlighted, plus one short line of text.

Only presents data an already-raised PositionOutOfRangeError /
TrajectoryOutOfRangeError carries — the validation logic itself
(trajectory_validator.py) is untouched by this module.
"""

from typing import Iterable, List, Sequence

from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from src.communication.protocol import Position, TrajectoryPoint
from src.controllers.system_state import CalibrationSpace
from src.ui.calibration_map_window import CalibrationMapView
from src.ui.style import (
    BUTTON_STYLE_PRIMARY, COLOR_TEXT, FONT_SIZE_NORMAL, LAYOUT_MARGIN,
    LAYOUT_SPACING, STATUS_COLORS,
)
from src.utils.trajectory_validator import (
    AxisViolation, PositionOutOfRangeError, TrajectoryOutOfRangeError,
)

# Maps (axis_label, exceeds_max) -> the CalibrationMapView boundary key
# it corresponds to (see AxisViolation.axis_label/.exceeds_max).
_AXIS_TO_BOUNDARY = {
    ("X", True): "x_max", ("X", False): "x_min",
    ("Y", True): "y_max", ("Y", False): "y_min",
    ("Ángulo", True): "angle_max", ("Ángulo", False): "angle_min",
}


def _boundary_keys(violations: Iterable[AxisViolation]) -> set:
    return {_AXIS_TO_BOUNDARY[(v.axis_label, v.exceeds_max)] for v in violations}


class LimitViolationDialog(QDialog):
    """
    Args:
        calibration_space: the space the rejected position/trajectory
            was checked against — needed to draw the map footprint.
        context_message: short, context-specific lead-in (e.g. "No se
            puede aplicar ese incremento manual" / "Ensayo rechazado").
            The axis/excess wording itself is generated here from the
            violation data, not part of this string.
        position: the single position to highlight on the map — for a
            full ensayo rejection, pass the FIRST offending point (see
            from_trajectory_error).
        violations: the AxisViolation(s) at that position.
        extra_note: optional one-line addendum (e.g. "y 2 punto(s) más
            fuera de rango") — still kept to one short line, never a
            paragraph.
    """

    def __init__(
        self,
        calibration_space: CalibrationSpace | None,
        context_message: str,
        position: Position,
        violations: Sequence[AxisViolation],
        extra_note: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Fuera del Espacio Disponible")
        self.setModal(True)

        root = QVBoxLayout(self)
        root.setSpacing(LAYOUT_SPACING)
        root.setContentsMargins(
            LAYOUT_MARGIN, LAYOUT_MARGIN, LAYOUT_MARGIN, LAYOUT_MARGIN
        )

        heading = QLabel(context_message)
        heading.setStyleSheet(
            f"font-size: {FONT_SIZE_NORMAL}px; font-weight: bold; "
            f"color: {STATUS_COLORS()['ERROR']};"
        )
        heading.setWordWrap(True)
        root.addWidget(heading)

        detail_text = "; ".join(v.describe() for v in violations)
        if extra_note:
            detail_text += f" ({extra_note})"
        detail = QLabel(detail_text)
        detail.setStyleSheet(f"font-size: {FONT_SIZE_NORMAL}px; color: {COLOR_TEXT()};")
        detail.setWordWrap(True)
        root.addWidget(detail)

        self._map_view = CalibrationMapView()
        self._map_view.set_calibration_space(calibration_space)
        self._map_view.set_violation(position, _boundary_keys(violations))
        root.addWidget(self._map_view, stretch=1)

        button_row = QHBoxLayout()
        button_row.addStretch()
        ok_button = QPushButton("Entendido y corregir")
        ok_button.setStyleSheet(BUTTON_STYLE_PRIMARY())
        ok_button.clicked.connect(self.accept)
        button_row.addWidget(ok_button)
        root.addLayout(button_row)

        # Fixed width, auto height: the detail text's length varies a
        # lot (one axis vs. three, one offending point vs. "y N más"),
        # and a plain adjustSize()/sizeHint() here under-counts a
        # word-wrapped QLabel's real height (verified empirically — it
        # falls back to a single-line estimate before the dialog has
        # ever been laid out at a real width), which pushed the button
        # down over the map. Fixing the width first lets
        # layout().heightForWidth() report the real wrapped height, so
        # the button always ends up below the map regardless of message
        # length.
        width = 440
        self.setFixedWidth(width)
        self.layout().activate()
        self.resize(width, self.layout().heightForWidth(width))

    @classmethod
    def from_position_error(
        cls,
        calibration_space: CalibrationSpace | None,
        context_message: str,
        target: Position,
        exc: PositionOutOfRangeError,
        parent=None,
    ) -> "LimitViolationDialog":
        """For a single rejected target — manual moves and the
        initial/return synchronized trajectories."""
        return cls(
            calibration_space, context_message, target, exc.violations, parent=parent
        )

    @classmethod
    def from_trajectory_error(
        cls,
        calibration_space: CalibrationSpace | None,
        context_message: str,
        points: List[TrajectoryPoint],
        exc: TrajectoryOutOfRangeError,
        parent=None,
    ) -> "LimitViolationDialog":
        """For a rejected CSV-loaded ensayo — highlights the FIRST
        offending point of possibly many (see exc.point_violations);
        `points` must be the same (already offset) list validation ran
        against, so the point's other axes can be read for the marker."""
        index, _t, violations = exc.point_violations[0]
        point = points[index]
        position = Position(x=point.x, y=point.y, angle=point.angle)
        remaining = len(exc.point_violations) - 1
        extra_note = (
            f"y {remaining} punto(s) más fuera de rango" if remaining > 0 else ""
        )
        return cls(
            calibration_space, context_message, position, violations,
            extra_note=extra_note, parent=parent,
        )
