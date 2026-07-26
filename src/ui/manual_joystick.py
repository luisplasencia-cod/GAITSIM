"""
manual_joystick.py

Directional joystick-style control for manual per-axis movement — moved
here from ConnectionScreen's "Movimiento Manual" box (2026-07-25+
redesign, Luis's request) and re-shaped as 4 straight arrows (X/Y) + 2
curved ones (angle) around a cycling step number, instead of a grid of
+/- buttons and 3 separate step-preset buttons. Lives as a floating
overlay in the top-right corner of TrajectoryScreen's platform
visualization (see trajectory_screen.py's _build_platform_panel).

Every action here is the SAME call ConnectionScreen used to make
(SystemStateMachine.move_relative(), same pre-validation against the
calibrated movement space via check_position()/LimitViolationDialog,
same background ActionWorker) — only the trigger widget and the step-
selection UI changed. This is a move, not a rewrite.
"""

from typing import Callable, Optional

from PySide6.QtCore import QEvent, QPoint
from PySide6.QtWidgets import QGridLayout, QPushButton, QWidget

from src.communication.protocol import Position
from src.controllers.initial_position_session import InitialPositionSession
from src.ui.action_worker import ActionWorker
from src.ui.bridge import StateMachineBridge
from src.ui.limit_violation_dialog import LimitViolationDialog
from src.ui.style import (
    JOYSTICK_BUTTON_STYLE, JOYSTICK_CENTER_BUTTON_STYLE, JOYSTICK_PANEL_STYLE,
)
from src.ui.theme_manager import ThemeManager
from src.utils.trajectory_validator import PositionOutOfRangeError, check_position

# Same 3 presets ConnectionScreen's old increment buttons offered — this
# is now the only place that selects one, cycled via the center button
# instead of 3 separate radio-style buttons.
STEP_VALUES = (1.0, 5.0, 10.0)

# Up=Y+, Down=Y-, Left=X-, Right=X+ — standard joystick convention
# (right/up = positive). Clockwise=A+, counter-clockwise=A- (arbitrary
# but consistent; trivial to swap if it reads backwards on the rig).
_DIRECTION_TO_AXIS = {
    "up": ("Y", "+"),
    "down": ("Y", "-"),
    "left": ("X", "-"),
    "right": ("X", "+"),
    "cw": ("A", "+"),
    "ccw": ("A", "-"),
}

# How far (px) a press has to move before it counts as a drag rather
# than a tap — below this, releasing still cycles the step as normal.
_DRAG_THRESHOLD_PX = 6


class _DraggableCenterButton(QPushButton):
    """
    The center step-cycling button doubles as the control's drag handle
    (Luis's explicit request — press-and-hold it to relocate the whole
    joystick anywhere within the platform view, since a fixed spot could
    end up covering something the operator needs to see). A plain tap
    (no meaningful movement between press and release) still cycles the
    step via the normal `clicked` signal; a press that moves past
    _DRAG_THRESHOLD_PX drags instead, and the eventual release is
    swallowed so it doesn't ALSO fire a click.
    """

    def __init__(self, text: str, joystick: "ManualJoystickControl"):
        super().__init__(text)
        self._joystick = joystick
        self._press_global_pos: Optional[QPoint] = None
        self._dragged = False

    def mousePressEvent(self, event):
        self._press_global_pos = event.globalPosition().toPoint()
        self._dragged = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._press_global_pos is not None:
            current = event.globalPosition().toPoint()
            delta = current - self._press_global_pos
            if not self._dragged and (
                abs(delta.x()) > _DRAG_THRESHOLD_PX or abs(delta.y()) > _DRAG_THRESHOLD_PX
            ):
                self._dragged = True
            if self._dragged:
                self._joystick.move_by(delta)
                self._press_global_pos = current
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        was_dragged = self._dragged
        self._press_global_pos = None
        self._dragged = False
        if was_dragged:
            event.accept()  # swallow — a drag release is not a click
            return
        super().mouseReleaseEvent(event)


class ManualJoystickControl(QWidget):
    """
    Args:
        bridge, position_session: same shared objects every screen uses.
        parent: the widget this floats on top of (top-right corner) —
                must be the actual visualization widget, not just any
                ancestor, since positioning tracks ITS resize events.
        theme_manager: shared ThemeManager — re-applies this control's
                own baked-in colors (panel background, button chrome)
                live when the operator toggles light/dark.
        on_status: optional callback for failure messages (there is no
                dedicated status label on this control itself — the
                caller, typically the host screen's own info label,
                decides where to show it).
    """

    def __init__(
        self,
        bridge: StateMachineBridge,
        position_session: InitialPositionSession,
        parent: QWidget,
        theme_manager: ThemeManager,
        on_status: Optional[Callable[[str], None]] = None,
    ):
        super().__init__(parent)
        self.setObjectName("manualJoystick")
        self._bridge = bridge
        self._position_session = position_session
        self._on_status = on_status
        self._worker = None
        self._step_index = 0  # cycles STEP_VALUES: 1 -> 5 -> 10 -> 1
        self._buttons = {}  # direction key -> QPushButton
        # True once the operator has dragged the control at least once —
        # from then on its position is THEIRS, so parent resizes must
        # not snap it back to the default corner (see eventFilter).
        self._user_positioned = False

        self._build_ui()
        self._apply_theme(theme_manager.name)
        theme_manager.theme_changed.connect(self._apply_theme)

        parent.installEventFilter(self)
        self._reposition()
        self.show()
        self.raise_()

    def _apply_theme(self, _name: str) -> None:
        self.setStyleSheet(JOYSTICK_PANEL_STYLE())
        for button in self._buttons.values():
            button.setStyleSheet(JOYSTICK_BUTTON_STYLE())
        self._center_button.setStyleSheet(JOYSTICK_CENTER_BUTTON_STYLE())

    def set_status_callback(self, callback: Optional[Callable[[str], None]]) -> None:
        """
        Lets the host screen wire this up after its own status widget
        exists — the constructor runs before TrajectoryScreen builds its
        info_label (this control is created while the platform panel is
        still being assembled), so on_status can't always be passed at
        construction time.
        """
        self._on_status = callback

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        layout = QGridLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)

        def make_button(label: str, direction: str) -> QPushButton:
            # Styled by _apply_theme() right after _build_ui() returns
            # (see __init__) — not here, so there is one place that
            # applies (and later re-applies) this button's color.
            btn = QPushButton(label)
            btn.clicked.connect(lambda checked=False, d=direction: self._on_direction(d))
            self._buttons[direction] = btn
            return btn

        layout.addWidget(make_button("↺", "ccw"), 0, 0)
        layout.addWidget(make_button("↑", "up"), 0, 1)
        layout.addWidget(make_button("↻", "cw"), 0, 2)

        layout.addWidget(make_button("←", "left"), 1, 0)

        self._center_button = _DraggableCenterButton(
            f"{STEP_VALUES[self._step_index]:g}", self
        )
        # Styled by _apply_theme() — see make_button()'s comment above.
        self._center_button.setToolTip(
            "Toca para cambiar el paso (1 / 5 / 10) — mantén presionado y "
            "arrastra para mover este control"
        )
        self._center_button.clicked.connect(self._cycle_step)
        layout.addWidget(self._center_button, 1, 1)

        layout.addWidget(make_button("→", "right"), 1, 2)
        layout.addWidget(make_button("↓", "down"), 2, 1)

    # ------------------------------------------------------------------
    # Step cycling — replaces the old 1/5/10 preset buttons, same values.
    # ------------------------------------------------------------------

    def _cycle_step(self):
        self._step_index = (self._step_index + 1) % len(STEP_VALUES)
        self._center_button.setText(f"{STEP_VALUES[self._step_index]:g}")

    @property
    def _step(self) -> float:
        return STEP_VALUES[self._step_index]

    # ------------------------------------------------------------------
    # Positioning: floats in the top-right corner of the parent widget
    # (the platform visualization canvas — see trajectory_screen.py) by
    # default. The operator can drag it anywhere within that same canvas
    # via the center button (see _DraggableCenterButton/move_by) — once
    # they have, _user_positioned keeps a later parent resize from
    # snapping it back to this default corner.
    # ------------------------------------------------------------------

    _DEFAULT_MARGIN = 12

    def _reposition(self):
        if self._user_positioned:
            return
        parent = self.parentWidget()
        if parent is None:
            return
        self.adjustSize()
        margin = self._DEFAULT_MARGIN
        self.move(max(parent.width() - self.width() - margin, 0), margin)

    def move_by(self, delta: QPoint) -> None:
        """Drag the whole control by `delta` (px), clamped so it always
        stays fully within the parent (the platform view canvas)."""
        parent = self.parentWidget()
        if parent is None:
            return
        self._user_positioned = True
        new_pos = self.pos() + delta
        max_x = max(parent.width() - self.width(), 0)
        max_y = max(parent.height() - self.height(), 0)
        x = min(max(new_pos.x(), 0), max_x)
        y = min(max(new_pos.y(), 0), max_y)
        self.move(x, y)

    def eventFilter(self, watched, event):
        if watched is self.parentWidget() and event.type() == QEvent.Resize:
            self._reposition()
        return super().eventFilter(watched, event)

    def showEvent(self, event):
        super().showEvent(event)
        self._reposition()
        self.raise_()

    # ------------------------------------------------------------------
    # Movement — identical logic to ConnectionScreen's former
    # _on_manual_move/_on_manual_move_succeeded (moved, not rewritten).
    # ------------------------------------------------------------------

    def _on_direction(self, direction: str):
        axis, move_dir = _DIRECTION_TO_AXIS[direction]
        amount = self._step

        sm = self._bridge.state_machine
        current = self._position_session.position
        space = sm.last_calibration_space
        if current is not None and space is not None:
            delta = amount if move_dir == "+" else -amount
            target = Position(
                x=current.x + delta if axis == "X" else current.x,
                y=current.y + delta if axis == "Y" else current.y,
                angle=current.angle + delta if axis == "A" else current.angle,
            )
            violations = check_position(target, space)
            if violations:
                LimitViolationDialog(
                    space, "No se puede aplicar ese incremento manual",
                    target, violations, parent=self,
                ).exec()
                return

        def move():
            try:
                sm.move_relative(axis, move_dir, amount)
            except PositionOutOfRangeError as exc:
                raise RuntimeError(
                    f"No se puede aplicar ese incremento manual: {exc}"
                ) from exc

        self._worker = ActionWorker(move)
        self._worker.succeeded.connect(
            lambda: self._on_move_succeeded(axis, move_dir, amount)
        )
        self._worker.failed.connect(self._on_move_failed)
        self._worker.start()

    def _on_move_succeeded(self, axis: str, direction: str, amount: float):
        self._position_session.apply_manual_delta(axis, direction, amount)
        self.refresh_controls()

    def _on_move_failed(self, message: str):
        if self._on_status is not None:
            self._on_status(f"Movimiento manual fallido: {message}")
        self.refresh_controls()

    # ------------------------------------------------------------------
    # Enable/disable — identical rule to ConnectionScreen's former
    # _refresh_controls (moved, not rewritten). Public: TrajectoryScreen
    # calls this from showEvent too, since InitialPositionSession is
    # plain data (no Qt signal), so a position change made on
    # ConnectionScreen while this screen is hidden needs an explicit
    # refresh once this screen becomes visible again.
    # ------------------------------------------------------------------

    def refresh_controls(self):
        sm = self._bridge.state_machine
        position = self._position_session.position

        can_move = sm.can_move_manually() and position is not None
        can_move_x = can_move and (
            abs(position.angle - sm.ANGLE_REFERENCE_DEG)
            <= sm.ANGLE_REFERENCE_TOLERANCE_DEG
        )

        for direction, (axis, _sign) in _DIRECTION_TO_AXIS.items():
            enabled = can_move_x if axis == "X" else can_move
            button = self._buttons[direction]
            button.setEnabled(enabled)
            if not can_move:
                tooltip = "Primero presiona 'Ir a Posición Inicial'."
            elif axis == "X" and not can_move_x:
                tooltip = "Endereza primero el ángulo (referencia) para poder mover X."
            else:
                tooltip = ""
            button.setToolTip(tooltip)
