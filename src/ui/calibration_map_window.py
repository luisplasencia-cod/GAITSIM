"""
calibration_map_window.py

Non-modal window that visualizes the available movement-space map
computed by a HOME's 3-axis limit-mapping sweep (see docs/protocol.md,
Calibration Events, and SystemStateMachine.CalibrationSpace in
system_state.py). Originally embedded as a panel inside
ConnectionScreen; moved out into its own window (same pattern as
Monitor Posición / monitor_3d_window.py) so the diagram has real room
to read instead of competing for space with the connection/manual-
movement controls.

Read-only: never sends commands, only reacts to StateMachineBridge
signals (calibration_limit/calibration_progress while HOMING) and
reads SystemStateMachine.last_calibration_space directly for its
starting state — so opening this window after a calibration already
completed this session still shows the right map immediately, not just
newly-arriving events.
"""

from PySide6.QtCore import Qt, QPointF, QRectF
from PySide6.QtGui import QBrush, QColor, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from src.controllers.system_state import CalibrationSpace
from src.ui.bridge import StateMachineBridge
from src.ui.style import (
    COLOR_SURFACE_ALT, COLOR_TEXT_MUTED, COLOR_AXIS_X, COLOR_AXIS_Y,
    COLOR_AXIS_ANGLE, FONT_SIZE_NORMAL,
)

# Spanish labels for calibration status messages, keyed by the axis
# codes used throughout the protocol (Y/X/A — see docs/protocol.md).
_AXIS_LABELS_ES = {"Y": "Y (vertical)", "X": "X (horizontal)", "A": "ángulo"}


class CalibrationMapView(QWidget):
    """
    Draws the available movement-space footprint from a completed
    calibration: the X x Y rectangle the platform can reach, plus the
    angular sweep. Uses the same per-axis colors as the live trajectory
    plot (trajectory_screen.py) and Monitor Posición
    (monitor_3d_window.py) so one color always means the same axis
    everywhere in the app.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(360, 360)
        self._space: CalibrationSpace | None = None

    def set_calibration_space(self, space: CalibrationSpace | None) -> None:
        self._space = space
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor(COLOR_SURFACE_ALT))

        if self._space is None:
            painter.setPen(QColor(COLOR_TEXT_MUTED))
            painter.drawText(self.rect(), Qt.AlignCenter, "Sin calibrar")
            painter.end()
            return

        space = self._space
        margin = 56
        avail_w = max(self.width() - 2 * margin, 1)
        avail_h = max(self.height() - 2 * margin, 1)
        x_range = max(space.x_range, 0.01)
        y_range = max(space.y_range, 0.01)

        # Fit the footprint rectangle to the widget, preserving the
        # real X:Y aspect ratio rather than stretching to fill.
        scale = min(avail_w / x_range, avail_h / y_range)
        rect_w = x_range * scale
        rect_h = y_range * scale
        rect_x = margin + (avail_w - rect_w) / 2
        rect_y = margin + (avail_h - rect_h) / 2
        footprint = QRectF(rect_x, rect_y, rect_w, rect_h)

        fill = QColor(COLOR_AXIS_X)
        fill.setAlpha(35)
        painter.setBrush(QBrush(fill))
        painter.setPen(QPen(QColor(COLOR_AXIS_X), 2))
        painter.drawRect(footprint)

        # Y ruler: vertical extent, left of the footprint.
        painter.setPen(QPen(QColor(COLOR_AXIS_Y), 2))
        painter.drawLine(
            QPointF(rect_x - 14, rect_y), QPointF(rect_x - 14, rect_y + rect_h)
        )
        painter.drawText(
            QRectF(0, rect_y - 12, rect_x - 20, 24),
            Qt.AlignRight | Qt.AlignVCenter, f"{space.y_range:.1f}",
        )
        painter.drawText(
            QRectF(0, rect_y + rect_h - 12, rect_x - 20, 24),
            Qt.AlignRight | Qt.AlignVCenter, "0",
        )

        # X ruler: horizontal extent, below the footprint.
        painter.setPen(QPen(QColor(COLOR_AXIS_X), 2))
        painter.drawText(
            QRectF(rect_x, rect_y + rect_h + 8, rect_w, 24), Qt.AlignLeft, "0",
        )
        painter.drawText(
            QRectF(rect_x, rect_y + rect_h + 8, rect_w, 24),
            Qt.AlignRight, f"{space.x_range:.1f} cm",
        )

        # Angle arc, centered on the footprint, sweeping the calibrated
        # angular range around a dashed 0°/horizontal reference line —
        # angle_min is typically negative (see CalibrationSpace's
        # docstring), so a bare total-span number would no longer say
        # where 0° actually falls within that sweep.
        arc_radius = min(rect_w, rect_h) * 0.3
        center = footprint.center()
        arc_rect = QRectF(
            center.x() - arc_radius, center.y() - arc_radius,
            arc_radius * 2, arc_radius * 2,
        )

        ref_pen = QPen(QColor(COLOR_TEXT_MUTED), 1, Qt.DashLine)
        painter.setPen(ref_pen)
        painter.drawLine(center, QPointF(center.x(), center.y() - arc_radius))

        # Qt angles: 0 = 3 o'clock, positive = counter-clockwise, in
        # 1/16th-of-a-degree units. Mapped so 0° (horizontal, the
        # dashed line above) points straight up and increasing angle
        # sweeps clockwise from there.
        start_qt = int((90 - space.angle_min) * 16)
        span_qt = -int(space.angle_range * 16)
        painter.setPen(QPen(QColor(COLOR_AXIS_ANGLE), 2))
        painter.setBrush(Qt.NoBrush)
        painter.drawArc(arc_rect, start_qt, span_qt)
        painter.drawText(
            QRectF(center.x() - 70, center.y() + arc_radius + 6, 140, 24),
            Qt.AlignCenter, f"{space.angle_min:.1f}° a {space.angle_max:.1f}°",
        )

        painter.end()


class CalibrationMapWindow(QWidget):
    """
    Args:
        bridge: The shared StateMachineBridge — read-only use here,
                same instance every other screen/window shares.
    """

    def __init__(self, bridge: StateMachineBridge, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Espacio Disponible — GAITSIM")
        self.resize(700, 700)
        self._bridge = bridge
        self._build_ui()
        self._connect_signals()
        # Reflect an already-completed calibration immediately if this
        # window is opened after HOME already succeeded this session,
        # rather than only reacting to newly-arriving events.
        self._apply_space(self._bridge.state_machine.last_calibration_space)

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        info_bar = QWidget()
        info_bar.setObjectName("headerBar")
        info_layout = QHBoxLayout(info_bar)
        info_layout.setContentsMargins(16, 10, 16, 10)

        self._status_label = QLabel("Sin calibrar")
        self._status_label.setStyleSheet(
            f"font-size: {FONT_SIZE_NORMAL}px; color: {COLOR_TEXT_MUTED};"
        )
        info_layout.addWidget(self._status_label)
        info_layout.addStretch()

        self._y_label = QLabel("Y: —")
        self._y_label.setStyleSheet(f"color: {COLOR_AXIS_Y}; font-weight: 600;")
        self._x_label = QLabel("X: —")
        self._x_label.setStyleSheet(f"color: {COLOR_AXIS_X}; font-weight: 600;")
        self._angle_label = QLabel("Ángulo: —")
        self._angle_label.setStyleSheet(f"color: {COLOR_AXIS_ANGLE}; font-weight: 600;")
        for label in (self._y_label, self._x_label, self._angle_label):
            info_layout.addWidget(label)
            info_layout.addSpacing(16)

        root.addWidget(info_bar)

        self._map_view = CalibrationMapView()
        root.addWidget(self._map_view, stretch=1)

    def _connect_signals(self):
        self._bridge.calibration_limit.connect(self._on_calibration_limit)
        self._bridge.calibration_progress.connect(self._on_calibration_progress)
        self._bridge.state_changed.connect(self._on_state_changed)

    def _apply_space(self, space: CalibrationSpace | None) -> None:
        self._map_view.set_calibration_space(space)
        if space is not None:
            self._y_label.setText(f"Y: {space.y_range:.1f} cm")
            self._x_label.setText(f"X: {space.x_range:.1f} cm")
            self._angle_label.setText(f"Ángulo: {space.angle_range:.1f}°")

    def _on_state_changed(self, state_name: str):
        # HOME's limit-mapping sweep ends with the HOMING -> IDLE
        # transition — refresh from the state machine's freshly-computed
        # CalibrationSpace rather than trying to track completion via
        # the individual LIM*/CAL_PROGRESS events themselves.
        if state_name == "IDLE":
            space = self._bridge.state_machine.last_calibration_space
            self._apply_space(space)
            if space is not None:
                self._status_label.setText("Calibración completada.")

    def _on_calibration_limit(self, axis: str, bound: str, value):
        label = _AXIS_LABELS_ES.get(axis, axis)
        which = "mínimo" if bound == "MIN" else "máximo"
        suffix = f" ({value:.1f})" if value is not None else ""
        self._status_label.setText(
            f"Calibrando eje {label}: límite {which} alcanzado{suffix}."
        )

    def _on_calibration_progress(self, axis: str, value: float):
        label = _AXIS_LABELS_ES.get(axis, axis)
        self._status_label.setText(f"Calibrando eje {label}... {value:.1f}")
