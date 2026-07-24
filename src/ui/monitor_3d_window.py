"""
monitor_3d_window.py

Non-modal monitoring window: renders the platform's real position
(X/Y translation, angular tilt) as reported by GET_POSITION, updated
live via polling. Built specifically to visually confirm that
SystemStateMachine.safe_return_to_position() actually behaves as
designed on real hardware — the move order (X, then angle, then Y) and
the "never below the previous trial's Y" safety rule — by watching it
happen, not just trusting the mocked-controller tests. This window is a
monitoring aid; SystemStateMachine remains the single source of truth,
this never sends commands, only reads position.

Rendering: a 2.5D side-view (QPainter — a rotated rectangle for the
platform, a shadow on the ground line to read height, a fixed HOME
marker) — NOT Qt3D/OpenGL. This was originally built with Qt3D per
Luis's choice, but Qt3D needs a real GPU/GL context that could not be
verified in the dev environment (segfaults there) and, per Luis,
produced nothing visible on the real hardware either. QPainter uses the
exact same rendering path as every other widget in this app (buttons,
the pyqtgraph plots) — guaranteed to actually render, which matters
more here than true 3D does: the goal is confirming the safety sequence
visually, not a faithful 3D model.

Also shows a fixed marker at the HOME reference origin (0, 0) as a
simple calibration view, since HOME establishes that reference every
session (and, since 2026-07-20, can only happen once per session — see
SystemStateMachine.can_home()).

Reworked 2026-07-21 for legibility (Luis reported the original render —
grid + generic rotated rectangle — "no se ve tan entendible"): a rotated
rectangle alone doesn't read as "tilt" at small angles, and there was no
explicit scale tying the drawing to actual cm/degree values. Each of the
3 DOF now has its own explicit, color-coded readout, using the SAME
X/Y/angle colors as the live trajectory plot (trajectory_screen.py) via
style.py's COLOR_AXIS_* constants, so one color always means the same
axis everywhere in the app: a horizontal cm ruler (X), a vertical height
guide from the ground line to the platform (Y), and an arc+needle
against a dashed "level" reference (angle). A short fading motion trail
was also added — the entire point of this window is watching
safe_return_to_position()'s multi-step sequence happen, which a single
static snapshot doesn't convey.

Why polling instead of relying solely on the signal: as of the safe-
return-trajectory rework, safe_return_to_position() DOES emit
TRAJ_PROGRESS/trajectory_progress internally now (it runs its 5-step
move through the same TRAJ_BEGIN/TRAJ_POINT/TRAJ_END + RUN protocol as
any gait trajectory, same as CalibrationMapWindow's live marker relies
on) — so a signal-only implementation is now possible in principle.
This window still polls GET_POSITION independently, since that was
already built/verified before the rework and gives an interval/trail
decoupled from whatever trajectory happens to be running (e.g. also
shows manual moves and plain GOTOs, which never emit TRAJ_PROGRESS).
Only runs while this window is visible.
"""

from collections import deque

from PySide6.QtCore import QPointF, QRectF, Qt, QThread, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from src.ui.bridge import StateMachineBridge
from src.ui.style import (
    COLOR_AXIS_ANGLE, COLOR_AXIS_X, COLOR_AXIS_Y,
    COLOR_BG, COLOR_BORDER, COLOR_TEXT, COLOR_TEXT_MUTED, FONT_SIZE_NORMAL,
)

POLL_INTERVAL_MS = 200

# Pixels per cm — a display placeholder (no exact rig dimensions
# available yet); tune once seen on real hardware.
PX_PER_CM = 3.0

PLATFORM_LENGTH_CM = 26.0   # visual width of the drawn platform
PLATFORM_THICKNESS_PX = 14  # visual thickness, in screen pixels (not scaled)

# How many past positions to keep for the fading motion trail — at the
# 200ms poll interval this covers roughly the last 10s, enough to see
# safe_return_to_position()'s multi-step sequence happen without the
# trail becoming visual noise from a long stationary period.
TRAIL_MAX_LEN = 50


class _PositionPoller(QThread):
    """Polls GET_POSITION at a fixed interval while running."""

    position_received = Signal(object)  # Position

    def __init__(self, state_machine, parent=None):
        super().__init__(parent)
        self._state_machine = state_machine
        self._running = False

    def run(self):
        self._running = True
        while self._running:
            try:
                position = self._state_machine.get_position()
                self.position_received.emit(position)
            except Exception:
                pass  # transient (e.g. mid-disconnect) — just skip this tick
            self.msleep(POLL_INTERVAL_MS)

    def stop(self):
        self._running = False
        self.wait(2 * POLL_INTERVAL_MS)


class _PlatformView(QWidget):
    """
    The actual drawing surface: a side-view of the platform moving in
    X/Y and tilting per the angle, plus a fixed HOME origin marker and
    a ground reference line. All via QPainter — no GPU dependency.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(560, 460)
        self._x_cm = 0.0
        self._y_cm = 0.0
        self._angle_deg = 0.0
        self._trail = deque(maxlen=TRAIL_MAX_LEN)

    def set_position(self, x_cm: float, y_cm: float, angle_deg: float):
        self._x_cm = x_cm
        self._y_cm = y_cm
        self._angle_deg = angle_deg
        self._trail.append((x_cm, y_cm))
        self.update()

    def _origin_screen_point(self) -> QPointF:
        # HOME (0, 0) sits horizontally centered, in the lower third of
        # the widget — leaves room above for Y to grow (up = positive,
        # matching the physical "lift" direction) and a little below
        # for the ground/ shadow.
        return QPointF(self.width() / 2.0, self.height() * 0.72)

    def _to_screen(self, x_cm: float, y_cm: float) -> QPointF:
        origin = self._origin_screen_point()
        return QPointF(
            origin.x() + x_cm * PX_PER_CM,
            origin.y() - y_cm * PX_PER_CM,  # screen Y grows downward, world Y grows upward
        )

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor(COLOR_BG))

        origin = self._origin_screen_point()
        center = self._to_screen(self._x_cm, self._y_cm)

        self._draw_grid(painter)
        self._draw_trail(painter)
        self._draw_ground_line(painter, origin)
        self._draw_x_ruler(painter, origin)
        self._draw_y_guide(painter, origin, center)
        self._draw_home_marker(painter, origin)
        self._draw_angle_indicator(painter, center)
        self._draw_platform(painter, center, origin)
        self._draw_legend(painter)

        painter.end()

    def _draw_trail(self, painter: QPainter):
        """Fading path of recent positions — makes the ORDER and
        DIRECTION of a multi-step move (e.g. safe_return_to_position's
        lift/rotate/traverse/descend sequence) visible as it happens,
        not just the current instantaneous pose."""
        points = list(self._trail)
        n = len(points)
        if n < 2:
            return
        for i in range(1, n):
            fade = i / (n - 1)  # 0 = oldest segment, 1 = newest
            pen = QPen(QColor(int(230 * fade), int(230 * fade), int(230 * fade), int(160 * fade)))
            pen.setWidth(2)
            painter.setPen(pen)
            p1 = self._to_screen(*points[i - 1])
            p2 = self._to_screen(*points[i])
            painter.drawLine(p1, p2)

    def _draw_x_ruler(self, painter: QPainter, origin: QPointF):
        """Horizontal cm scale along the ground line, colored to match
        the X readout — an explicit numeric scale instead of only the
        generic background grid, so horizontal displacement from HOME
        reads directly off the drawing."""
        minor_step_cm = 10
        major_step_cm = 20
        left_cm = -origin.x() / PX_PER_CM
        right_cm = (self.width() - origin.x()) / PX_PER_CM
        start = int(left_cm // minor_step_cm) * minor_step_cm
        end = int(right_cm // minor_step_cm + 1) * minor_step_cm

        for cm in range(start, end + 1, minor_step_cm):
            x_px = origin.x() + cm * PX_PER_CM
            is_major = cm % major_step_cm == 0
            tick_h = 10 if is_major else 5
            pen = QPen(QColor(COLOR_AXIS_X))
            pen.setWidth(2 if is_major else 1)
            painter.setPen(pen)
            painter.drawLine(QPointF(x_px, origin.y() - tick_h), QPointF(x_px, origin.y() + tick_h))
            if is_major:
                painter.drawText(
                    QRectF(x_px - 20, origin.y() + tick_h + 2, 40, 16),
                    Qt.AlignCenter, f"{cm:g}",
                )

    def _draw_y_guide(self, painter: QPainter, origin: QPointF, center: QPointF):
        """Vertical guide from the ground line up to the platform,
        colored to match the Y readout — replaces the old shadow-only
        height cue with an explicit measurement. No floating number
        here (the header already gives the exact value in the same
        color) — a label anchored to the platform would otherwise
        drift into the angle indicator or off past the ruler as the
        platform moves, which is more clutter than clarity."""
        ground_point = QPointF(center.x(), origin.y())
        pen = QPen(QColor(COLOR_AXIS_Y))
        pen.setStyle(Qt.DashLine)
        pen.setWidth(2)
        painter.setPen(pen)
        painter.drawLine(ground_point, center)

        pen.setStyle(Qt.SolidLine)
        painter.setPen(pen)
        painter.drawLine(
            QPointF(ground_point.x() - 6, ground_point.y()),
            QPointF(ground_point.x() + 6, ground_point.y()),
        )

    def _draw_angle_indicator(self, painter: QPainter, center: QPointF):
        """Arc + needle against a dashed 0° reference at the platform's
        pivot, colored to match the angle readout. A rotated thin
        rectangle alone is too subtle to read tilt from at small
        angles — this gives the angle its own explicit encoding, using
        the exact same rotate(-angle) convention as the platform itself
        so the two always visually agree. Same reasoning as
        _draw_y_guide for not also drawing a floating degree label
        here — the header already shows it, in the same color."""
        radius = 65.0
        painter.save()
        painter.translate(center)

        ref_pen = QPen(QColor(COLOR_TEXT_MUTED))
        ref_pen.setStyle(Qt.DashLine)
        painter.setPen(ref_pen)
        painter.drawLine(QPointF(0, 0), QPointF(radius * 1.3, 0))

        needle_pen = QPen(QColor(COLOR_AXIS_ANGLE), 2)
        painter.setPen(needle_pen)
        painter.save()
        painter.rotate(-self._angle_deg)
        painter.drawLine(QPointF(0, 0), QPointF(radius, 0))
        painter.restore()

        arc_rect = QRectF(-radius, -radius, 2 * radius, 2 * radius)
        painter.drawArc(arc_rect, 0, int(-self._angle_deg * 16))

        painter.restore()

    def _draw_legend(self, painter: QPainter):
        """Fixed-position key (top-left, never moves or overlaps the
        platform) explaining what each color-coded visual element
        means — needed once, since the ruler/guide/arc themselves
        carry no text of their own now that per-frame labels were
        removed as clutter (see _draw_y_guide/_draw_angle_indicator)."""
        entries = [
            (COLOR_AXIS_X, "— posición horizontal (X)"),
            (COLOR_AXIS_Y, "┆ altura (Y)"),
            (COLOR_AXIS_ANGLE, "◠ inclinación (ángulo)"),
        ]
        x, y = 10, 10
        row_h = 18
        painter.setBrush(QBrush(QColor(0, 0, 0, 110)))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(QRectF(x - 6, y - 4, 210, row_h * len(entries) + 8), 4, 4)
        for i, (color, text) in enumerate(entries):
            painter.setPen(QColor(color))
            painter.drawText(QRectF(x, y + i * row_h, 200, row_h), Qt.AlignVCenter, text)

    def _draw_grid(self, painter: QPainter):
        pen = QPen(QColor(COLOR_BORDER))
        pen.setWidth(1)
        painter.setPen(pen)
        step = int(10 * PX_PER_CM)  # a line every 10cm
        if step <= 0:
            return
        for gx in range(0, self.width(), step):
            painter.drawLine(gx, 0, gx, self.height())
        for gy in range(0, self.height(), step):
            painter.drawLine(0, gy, self.width(), gy)

    def _draw_ground_line(self, painter: QPainter, origin: QPointF):
        pen = QPen(QColor(COLOR_TEXT_MUTED))
        pen.setWidth(2)
        pen.setStyle(Qt.DashLine)
        painter.setPen(pen)
        painter.drawLine(QPointF(0, origin.y()), QPointF(self.width(), origin.y()))
        painter.setPen(QColor(COLOR_TEXT_MUTED))
        painter.drawText(
            QRectF(8, origin.y() + 4, 200, 20), Qt.AlignLeft, "Y = 0 (piso de referencia)"
        )

    def _draw_home_marker(self, painter: QPainter, origin: QPointF):
        # Fixed at (0, 0) — the calibration view: distance from this
        # marker to the platform is a direct visual read of "how far
        # from HOME" at any moment. Kept off the X/Y/angle palette
        # (COLOR_TEXT, not COLOR_AXIS_X) so it never gets mistaken for
        # one of the 3 axis readouts.
        radius = 7
        painter.setPen(QPen(QColor(COLOR_TEXT), 2))
        painter.setBrush(QBrush(QColor(COLOR_TEXT)))
        painter.drawEllipse(origin, radius, radius)
        painter.setPen(QColor(COLOR_TEXT_MUTED))
        # Above the marker (not to the side) so it doesn't collide with
        # the platform when it's near HOME — the common case right
        # after homing or when returning to a nearby initial position.
        painter.drawText(
            QRectF(origin.x() - 60, origin.y() - 44, 120, 20),
            Qt.AlignCenter, "HOME (0, 0)",
        )

    def _draw_platform(self, painter: QPainter, center: QPointF, origin: QPointF):
        # Shadow on the ground line directly below the platform's X —
        # a cheap, effective way to read "how high above the floor"
        # without true 3D: the gap between platform and shadow IS the
        # height, visually.
        shadow_center = QPointF(center.x(), origin.y())
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(0, 0, 0, 90)))
        painter.drawEllipse(shadow_center, PLATFORM_LENGTH_CM * PX_PER_CM * 0.35, 6)

        half_len_px = (PLATFORM_LENGTH_CM * PX_PER_CM) / 2.0
        half_thick_px = PLATFORM_THICKNESS_PX / 2.0

        painter.save()
        painter.translate(center)
        # Screen-space rotation is clockwise-positive; negate so a
        # positive angle tilts the same visual direction as the
        # physical "up" convention used throughout the app.
        painter.rotate(-self._angle_deg)

        rect = QRectF(-half_len_px, -half_thick_px, 2 * half_len_px, 2 * half_thick_px)
        painter.setPen(QPen(QColor(COLOR_BORDER), 1))
        painter.setBrush(QBrush(QColor("#e5c53f")))
        painter.drawRoundedRect(rect, 4, 4)

        # A thin darker underside strip suggests thickness/depth without
        # needing an actual 3rd dimension.
        underside = QRectF(-half_len_px, half_thick_px - 3, 2 * half_len_px, 4)
        painter.setBrush(QBrush(QColor("#b89a2f")))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(underside, 2, 2)

        painter.restore()


class Monitor3DWindow(QWidget):
    """
    Args:
        bridge: The shared StateMachineBridge — read-only use here
                (get_position() via the state machine), same instance
                every other screen shares.
    """

    def __init__(self, bridge: StateMachineBridge, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Monitor de Posición — GAITSIM")
        self.resize(900, 650)
        self._bridge = bridge
        self._poller = None
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        info_bar = QWidget()
        info_bar.setObjectName("headerBar")
        info_layout = QHBoxLayout(info_bar)
        info_layout.setContentsMargins(16, 10, 16, 10)

        self._position_label = QLabel(self._format_position_text(0.0, 0.0, 0.0))
        self._position_label.setTextFormat(Qt.RichText)
        self._position_label.setStyleSheet(f"font-size: {FONT_SIZE_NORMAL}px;")
        info_layout.addWidget(self._position_label)
        info_layout.addStretch()

        self._calibration_label = QLabel("Referencia HOME: X=0, Y=0 (punto blanco)")
        self._calibration_label.setStyleSheet(
            f"font-size: {FONT_SIZE_NORMAL}px; color: {COLOR_TEXT_MUTED};"
        )
        info_layout.addWidget(self._calibration_label)
        root.addWidget(info_bar)

        self._platform_view = _PlatformView()
        root.addWidget(self._platform_view, stretch=1)

    @staticmethod
    def _format_position_text(x: float, y: float, angle: float) -> str:
        # Colored to match the drawing's X/Y/angle encoding (and the
        # live trajectory plot's palette) — same axis, same color,
        # everywhere in the app.
        return (
            f'<span style="color:{COLOR_AXIS_X}">X:</span> {x:.1f} cm &nbsp;&nbsp; '
            f'<span style="color:{COLOR_AXIS_Y}">Y:</span> {y:.1f} cm &nbsp;&nbsp; '
            f'<span style="color:{COLOR_AXIS_ANGLE}">Á:</span> {angle:.1f}°'
        )

    def _on_position_received(self, position):
        self._platform_view.set_position(position.x, position.y, position.angle)
        self._position_label.setText(
            self._format_position_text(position.x, position.y, position.angle)
        )

    def showEvent(self, event):
        super().showEvent(event)
        if self._poller is None:
            self._poller = _PositionPoller(self._bridge.state_machine, self)
            self._poller.position_received.connect(self._on_position_received)
            self._poller.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        if self._poller is not None:
            self._poller.stop()
            self._poller = None

    def closeEvent(self, event):
        if self._poller is not None:
            self._poller.stop()
            self._poller = None
        super().closeEvent(event)
