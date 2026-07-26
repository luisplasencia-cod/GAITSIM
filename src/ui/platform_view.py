"""
platform_view.py

PlatformView + PositionPoller: renders the platform's real position
(X/Y translation, angular tilt) as reported by GET_POSITION, updated
live via polling. Built specifically to visually confirm that
SystemStateMachine.safe_return_to_position() actually behaves as
designed on real hardware — the move order (X, then angle, then Y) and
the "never below the previous trial's Y" safety rule — by watching it
happen, not just trusting the mocked-controller tests. This is a
monitoring aid; SystemStateMachine remains the single source of truth,
this never sends commands, only reads position.

Originally its own top-level window ("Monitor Posición",
monitor_3d_window.py/Monitor3DWindow) opened from a header button.
Consolidated 2026-07-25 directly into trajectory_screen.py as the
dominant central element of that screen (Luis's request — one window
instead of three), alongside the trajectory selection/execution
controls and a shrunk reference-only copy of the live trajectory plot.
This module kept its rendering classes (PlatformView, PositionPoller)
unchanged; only the window shell (Monitor3DWindow) was removed, since
trajectory_screen.py now builds its own info bar and owns the poller's
lifecycle (start on show, stop on hide) directly.

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

Integrated with "Espacio Disponible" (2026-07-24): the reachable-space
rectangle + angular sweep from calibration_map_window.py's
CalibrationSpace is now ALSO drawn as a background layer directly on
this same canvas (PlatformView._draw_calibration_bounds), reusing this
view's own coordinate transform and angle convention rather than a
second overlaid widget — see that method's docstring for why.
calibration_map_window.py itself is untouched and still works standalone;
this is an addition, not a replacement. Before the first HOME this
session, the drawing/scale is bit-for-bit identical to before this was
added (fixed PX_PER_CM, centered origin); once calibrated, scale/origin
adapt to fit the known rectangle (see _effective_scale_and_origin).
"""

from collections import deque

from PySide6.QtCore import QPointF, QRectF, Qt, QThread, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

from src.controllers.system_state import CalibrationSpace
from src.ui.style import (
    COLOR_AXIS_ANGLE, COLOR_AXIS_X, COLOR_AXIS_Y,
    COLOR_BG, COLOR_BORDER, COLOR_TEXT, COLOR_TEXT_MUTED,
    COLOR_PLATFORM_FILL, COLOR_PLATFORM_UNDERSIDE,
)

POLL_INTERVAL_MS = 200

# Pixels per cm — a display placeholder (no exact rig dimensions
# available yet); tune once seen on real hardware.
PX_PER_CM = 3.0

PLATFORM_LENGTH_CM = 26.0   # visual width of the drawn platform
PLATFORM_THICKNESS_PX = 14  # visual thickness, in screen pixels (not scaled)

# Margin (px) kept around the fitted calibrated rectangle, once a
# CalibrationSpace is known — see PlatformView._effective_scale_and_origin.
CALIBRATION_FIT_MARGIN_PX = 50

# How many past positions to keep for the fading motion trail — at the
# 200ms poll interval this covers roughly the last 10s, enough to see
# safe_return_to_position()'s multi-step sequence happen without the
# trail becoming visual noise from a long stationary period.
TRAIL_MAX_LEN = 50


class PositionPoller(QThread):
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


class PlatformView(QWidget):
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
        # None until a HOME completes this session — see
        # set_calibration_space/_effective_scale_and_origin.
        self._space: CalibrationSpace | None = None

    def set_position(self, x_cm: float, y_cm: float, angle_deg: float):
        self._x_cm = x_cm
        self._y_cm = y_cm
        self._angle_deg = angle_deg
        self._trail.append((x_cm, y_cm))
        self.update()

    def set_calibration_space(self, space: CalibrationSpace | None) -> None:
        """
        Reachable-space overlay ("Espacio Disponible", integrated into
        this same canvas — see calibration_map_window.py for the
        original standalone view, unchanged and still usable on its
        own). None before the first HOME completes this session; the
        drawing/scale then behaves exactly as before this was added.
        """
        self._space = space
        self.update()

    def _effective_scale_and_origin(self) -> tuple[float, QPointF]:
        """
        Pixels-per-cm and the screen point representing HOME (0, 0),
        i.e. the origin every _draw_* method anchors to.

        Before calibration (self._space is None): the ORIGINAL fixed
        behavior, unchanged — PX_PER_CM, horizontally centered. This is
        what every existing flow (manual move, safe return, etc.) saw
        before this method existed.

        Once calibrated: scale is fit so the WHOLE calibrated rectangle
        (x_range x y_range) is visible (same fit-to-widget approach as
        CalibrationMapView), and the origin moves from centered to a
        small left margin. This isn't just cosmetic parity with the
        pre-calibration case: CalibrationSpace.x_min/y_min are ALWAYS
        0.0 by definition (that axis's own zero — see CalibrationSpace's
        docstring), meaning HOME sits at the near/bottom-left corner of
        the reachable rectangle, never inside it. Keeping the origin
        horizontally centered would waste the entire left half of the
        canvas once the real (previously unknown) X range is known.
        """
        if self._space is None:
            return PX_PER_CM, QPointF(self.width() / 2.0, self.height() * 0.72)

        origin_y_fraction = 0.72  # unchanged from the pre-calibration placement
        origin = QPointF(CALIBRATION_FIT_MARGIN_PX, self.height() * origin_y_fraction)

        x_range = max(self._space.x_range, 0.01)
        y_range = max(self._space.y_range, 0.01)
        avail_w = max(self.width() - 2 * CALIBRATION_FIT_MARGIN_PX, 1)
        avail_h = max(origin.y() - CALIBRATION_FIT_MARGIN_PX, 1)
        scale = min(avail_w / x_range, avail_h / y_range)
        return scale, origin

    def _origin_screen_point(self) -> QPointF:
        return self._effective_scale_and_origin()[1]

    def _to_screen(self, x_cm: float, y_cm: float) -> QPointF:
        scale, origin = self._effective_scale_and_origin()
        return QPointF(
            origin.x() + x_cm * scale,
            origin.y() - y_cm * scale,  # screen Y grows downward, world Y grows upward
        )

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor(COLOR_BG()))

        origin = self._origin_screen_point()
        center = self._to_screen(self._x_cm, self._y_cm)

        self._draw_grid(painter)
        self._draw_calibration_bounds(painter, origin, center)
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

    def _draw_calibration_bounds(self, painter: QPainter, origin: QPointF, center: QPointF):
        """
        Background layer: the reachable X x Y rectangle and angular
        sweep from the most recent HOME calibration
        (SystemStateMachine.CalibrationSpace) — the "Espacio
        Disponible" information integrated directly into this canvas
        (see calibration_map_window.py for the original standalone
        view, unchanged and still usable on its own).

        Drawn using this SAME view's _to_screen() transform, so it can
        never visually drift from the live marker/trail — no separate
        scale/origin calculation to keep in sync. No-op before the
        first HOME (self._space is None).
        """
        if self._space is None:
            return
        space = self._space

        # x_min/y_min are always 0.0 (== HOME — see CalibrationSpace's
        # docstring), so the near corner of this rectangle IS `origin`.
        far = self._to_screen(space.x_max, space.y_max)
        rect = QRectF(origin.x(), far.y(), far.x() - origin.x(), origin.y() - far.y())

        fill = QColor(COLOR_AXIS_X())
        fill.setAlpha(25)
        painter.setBrush(QBrush(fill))
        pen = QPen(QColor(COLOR_AXIS_X()))
        pen.setStyle(Qt.DashLine)
        pen.setWidth(1)
        painter.setPen(pen)
        painter.drawRect(rect)

        # Calibrated angular sweep, as a static background arc at the
        # platform's pivot — reuses the EXACT SAME Qt-angle convention
        # as _draw_angle_indicator's live needle/arc (0 = local +x
        # reference line, positive span = -angle*16), generalized from
        # a single current angle to the full [angle_min, angle_max]
        # range, so the two are guaranteed sign-consistent within this
        # one canvas (deliberately NOT reusing CalibrationMapView's
        # separate "0=up" arc math, which would mix two different
        # angle-zero conventions in the same drawing).
        radius = 65.0 * 1.15
        arc_rect = QRectF(center.x() - radius, center.y() - radius, 2 * radius, 2 * radius)
        arc_pen = QPen(QColor(COLOR_AXIS_ANGLE()))
        arc_pen.setStyle(Qt.DashLine)
        arc_pen.setWidth(1)
        painter.setPen(arc_pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawArc(arc_rect, int(-space.angle_min * 16), int(-space.angle_range * 16))

    def _draw_x_ruler(self, painter: QPainter, origin: QPointF):
        """Horizontal cm scale along the ground line, colored to match
        the X readout — an explicit numeric scale instead of only the
        generic background grid, so horizontal displacement from HOME
        reads directly off the drawing."""
        scale, _ = self._effective_scale_and_origin()
        minor_step_cm = 10
        major_step_cm = 20
        left_cm = -origin.x() / scale
        right_cm = (self.width() - origin.x()) / scale
        start = int(left_cm // minor_step_cm) * minor_step_cm
        end = int(right_cm // minor_step_cm + 1) * minor_step_cm

        for cm in range(start, end + 1, minor_step_cm):
            x_px = origin.x() + cm * scale
            is_major = cm % major_step_cm == 0
            tick_h = 10 if is_major else 5
            pen = QPen(QColor(COLOR_AXIS_X()))
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
        pen = QPen(QColor(COLOR_AXIS_Y()))
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

        ref_pen = QPen(QColor(COLOR_TEXT_MUTED()))
        ref_pen.setStyle(Qt.DashLine)
        painter.setPen(ref_pen)
        painter.drawLine(QPointF(0, 0), QPointF(radius * 1.3, 0))

        needle_pen = QPen(QColor(COLOR_AXIS_ANGLE()), 2)
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
            (COLOR_AXIS_X(), "— posición horizontal (X)"),
            (COLOR_AXIS_Y(), "┆ altura (Y)"),
            (COLOR_AXIS_ANGLE(), "◠ inclinación (ángulo)"),
        ]
        if self._space is not None:
            entries.append((COLOR_AXIS_X(), "▭ límite calibrado (Espacio Disponible)"))
        x, y = 10, 10
        row_h = 18
        # Box width fits the widest entry — a fixed 210px (enough for the
        # original 3 short entries) overflowed once the longer calibrated-
        # bounds entry was added.
        text_width = max(
            painter.fontMetrics().horizontalAdvance(text) for _, text in entries
        )
        box_width = text_width + 16
        painter.setBrush(QBrush(QColor(0, 0, 0, 110)))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(QRectF(x - 6, y - 4, box_width, row_h * len(entries) + 8), 4, 4)
        for i, (color, text) in enumerate(entries):
            painter.setPen(QColor(color))
            painter.drawText(QRectF(x, y + i * row_h, text_width, row_h), Qt.AlignVCenter, text)

    def _draw_grid(self, painter: QPainter):
        pen = QPen(QColor(COLOR_BORDER()))
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
        pen = QPen(QColor(COLOR_TEXT_MUTED()))
        pen.setWidth(2)
        pen.setStyle(Qt.DashLine)
        painter.setPen(pen)
        painter.drawLine(QPointF(0, origin.y()), QPointF(self.width(), origin.y()))
        painter.setPen(QColor(COLOR_TEXT_MUTED()))
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
        painter.setPen(QPen(QColor(COLOR_TEXT()), 2))
        painter.setBrush(QBrush(QColor(COLOR_TEXT())))
        painter.drawEllipse(origin, radius, radius)
        painter.setPen(QColor(COLOR_TEXT_MUTED()))
        # Above the marker (not to the side) so it doesn't collide with
        # the platform when it's near HOME — the common case right
        # after homing or when returning to a nearby initial position.
        painter.drawText(
            QRectF(origin.x() - 60, origin.y() - 44, 120, 20),
            Qt.AlignCenter, "HOME (0, 0)",
        )

    def _draw_platform(self, painter: QPainter, center: QPointF, origin: QPointF):
        # Sized from the CURRENT effective scale (not the fixed
        # PX_PER_CM constant) so the drawn platform stays proportionally
        # correct once calibration data fits the view to a different
        # scale — otherwise it would look wrong-sized relative to the
        # ruler/bounds once scale != PX_PER_CM.
        scale, _ = self._effective_scale_and_origin()

        # Shadow on the ground line directly below the platform's X —
        # a cheap, effective way to read "how high above the floor"
        # without true 3D: the gap between platform and shadow IS the
        # height, visually.
        shadow_center = QPointF(center.x(), origin.y())
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(0, 0, 0, 90)))
        painter.drawEllipse(shadow_center, PLATFORM_LENGTH_CM * scale * 0.35, 6)

        half_len_px = (PLATFORM_LENGTH_CM * scale) / 2.0
        half_thick_px = PLATFORM_THICKNESS_PX / 2.0

        painter.save()
        painter.translate(center)
        # Screen-space rotation is clockwise-positive; negate so a
        # positive angle tilts the same visual direction as the
        # physical "up" convention used throughout the app.
        painter.rotate(-self._angle_deg)

        rect = QRectF(-half_len_px, -half_thick_px, 2 * half_len_px, 2 * half_thick_px)
        painter.setPen(QPen(QColor(COLOR_BORDER()), 1))
        painter.setBrush(QBrush(QColor(COLOR_PLATFORM_FILL())))
        painter.drawRoundedRect(rect, 4, 4)

        # A thin darker underside strip suggests thickness/depth without
        # needing an actual 3rd dimension.
        underside = QRectF(-half_len_px, half_thick_px - 3, 2 * half_len_px, 4)
        painter.setBrush(QBrush(QColor(COLOR_PLATFORM_UNDERSIDE())))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(underside, 2, 2)

        painter.restore()
