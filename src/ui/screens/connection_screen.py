"""
connection_screen.py

First screen: serial connection, homing, and initial-position setup.
Follows the pattern used throughout the project: this screen never
talks to ESP32Controller directly — it goes through SystemStateMachine
(via StateMachineBridge) so permission logic (can_home(), etc.) stays
centralized in one place.

Manual per-axis movement used to live here too ("Movimiento Manual");
moved to TrajectoryScreen (2026-07-25+ redesign, Luis's request) as a
directional joystick control overlaid on the platform visualization —
see src/ui/manual_joystick.py. Nothing about how a move is validated or
executed changed, only which screen/widget triggers it.

Initial-position bookkeeping (which saved file, if any, the current
fields correspond to) is written to a shared InitialPositionSession so
TrajectoryScreen can apply it as an offset and, once a run finishes,
offer to save it — see src/controllers/initial_position_session.py.
"""

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QDoubleValidator, QFont, QFontMetrics
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QLabel, QLineEdit, QGroupBox, QComboBox,
    QInputDialog, QMessageBox
)

from src.communication.protocol import Position
from src.controllers.initial_position_session import InitialPositionSession
from src.ui.action_worker import ActionWorker
from src.ui.bridge import StateMachineBridge
from src.ui.numeric_keypad import NumericKeypad
from src.ui.theme_manager import ThemeManager
from src.ui.style import (
    BUTTON_STYLE, BUTTON_STYLE_COMPACT,
    LAYOUT_SPACING, LAYOUT_MARGIN, FONT_SIZE_STATUS,
    INPUT_STYLE,
)
from src.ui.limit_violation_dialog import LimitViolationDialog
from src.utils import position_library, trajectory_generator
from src.utils.trajectory_validator import PositionOutOfRangeError, check_position


class _AutoFitLabel(QLabel):
    """
    A QLabel that keeps its FULL text visible — word-wrapped, and with
    its own font size shrunk (within a reasonable floor) as needed —
    instead of eliding with "..." (the previous approach here). Luis
    asked for every message to stay fully readable rather than
    truncated, while the parent QGroupBox (ESTADO DEL SISTEMA) must
    still never grow past its fixed size — so this only ever adapts
    ITSELF (wrap + font size) to whatever fixed space it's given,
    never the other way around. Scoped to that one box — it was the
    only widget in the app that actually overflowed with longer
    messages.
    """

    _MAX_FONT_PX = FONT_SIZE_STATUS
    _MIN_FONT_PX = 11  # below this, text becomes hard to read on the
                        # touchscreen — the floor past which we accept
                        # the (rare, very long) message just clipping
                        # rather than shrinking further.

    def __init__(self, text: str = "", parent=None):
        self._full_text = ""
        super().__init__(parent)
        self.setWordWrap(True)
        self._apply_font_size(self._MAX_FONT_PX)
        self.setText(text)

    def setText(self, text: str) -> None:
        self._full_text = text
        self.setToolTip(text)
        super().setText(text)
        self._refit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Re-fit against the new width/height — setText() alone can't
        # react to a later resize (width()/height() are still 0 the
        # first time setText() runs, before the layout has settled).
        self._refit()

    def _apply_font_size(self, size_px: int) -> None:
        self.setStyleSheet(f"font-size: {size_px}px; font-weight: bold;")

    def _refit(self) -> None:
        if not self._full_text or self.width() <= 0 or self.height() <= 0:
            return
        # Measure with an independent QFont (not self.font()), since
        # the widget's own font may not yet reflect the stylesheet set
        # by a previous _apply_font_size() call at this point.
        base_font = QFont(self.font())
        for size in range(self._MAX_FONT_PX, self._MIN_FONT_PX - 1, -1):
            base_font.setPixelSize(size)
            metrics = QFontMetrics(base_font)
            bounds = metrics.boundingRect(
                0, 0, self.width(), 100_000, Qt.TextWordWrap, self._full_text
            )
            if bounds.height() <= self.height():
                self._apply_font_size(size)
                return
        # Even the floor size doesn't fit: use it anyway (best effort —
        # QLabel clips to its own rect rather than overflowing into
        # neighboring widgets, so the box's fixed size is still safe).
        self._apply_font_size(self._MIN_FONT_PX)


class ConnectionScreen(QWidget):
    """
    Screen for establishing the serial connection, homing, and setting
    up the initial position.

    Args:
        bridge: The shared StateMachineBridge instance (created once
                in main_window.py and passed to every screen).
        position_session: The shared InitialPositionSession (also
                owned by main_window.py), so TrajectoryScreen can see
                what this screen sets up.
        theme_manager: Shared ThemeManager — passed straight through to
                NumericKeypad, the only widget on this screen that bakes
                a color into its own stylesheet (this screen's own
                buttons are all sizing-only styles, so they need no
                re-application on a theme change — see style.py's
                module docstring).
    """

    # Emitted as soon as "Ir a Posición Inicial" actually starts a real
    # movement (not on a rejected/out-of-range attempt, and not on the
    # no-op case where the target is already the current position) — NOT
    # once it finishes, so the Monitor screen's live position polling is
    # visible for the whole move instead of only after there's nothing
    # left to watch. main_window.py connects this to switching to the
    # Monitor screen (2026-07-26+, Luis's explicit request; navigate-at-
    # start behavior since 2026-07-31), same cross-screen-navigation
    # pattern as TrajectoryScreen.request_new_trial.
    request_show_monitor = Signal()

    def __init__(
        self,
        bridge: StateMachineBridge,
        position_session: InitialPositionSession,
        theme_manager: ThemeManager,
        parent=None,
    ):
        super().__init__(parent)
        self._bridge = bridge
        self._position_session = position_session
        self._worker = None  # keeps a reference so the QThread isn't GC'd mid-run

        # True while a synchronized (0,0,0) -> initial-position trajectory
        # (see _on_goto_initial_synchronized) is running via send_trajectory()
        # + run(). Guards _on_trajectory_finished so it only reacts to THIS
        # screen's own move, not a gait trajectory finishing over on
        # TrajectoryScreen — both arrive on the same shared bridge signal.
        self._awaiting_initial_move = False
        self._pending_initial_position: Position | None = None

        # Shared popup numeric keypad for the 3 initial-position fields
        # (see _build_ui, where they're wired via installEventFilter) —
        # one instance, redirected to whichever field last gained focus.
        self._keypad = NumericKeypad(self, theme_manager)

        self._build_ui()
        self._connect_signals()
        self._refresh_position_list()
        self._refresh_controls()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        # Two rows of side-by-side boxes instead of one long vertical
        # stack — with 4 group boxes, a single column no longer fits
        # the 1280x800 touchscreen (see style.py) without clipping the
        # bottom controls. Using the screen's full width instead keeps
        # everything visible without scrolling.
        root = QVBoxLayout(self)
        root.setSpacing(LAYOUT_SPACING)
        root.setContentsMargins(
            LAYOUT_MARGIN, LAYOUT_MARGIN, LAYOUT_MARGIN, LAYOUT_MARGIN
        )

        # --- Status box ---
        status_box = QGroupBox("ESTADO DEL SISTEMA")
        status_layout = QHBoxLayout(status_box)

        # Font size/style is managed internally by _AutoFitLabel itself
        # (shrinks as needed to keep the full message visible) — no
        # external setStyleSheet() here, that would just be overwritten
        # on the next setText()/resizeEvent() anyway.
        self.status_label = _AutoFitLabel("DISCONNECTED")
        status_layout.addWidget(self.status_label)

        # --- Connection / Home box ---
        # "Conectar" used to live here as its own button; it was removed
        # (2026-07-25+ kiosk pass) in favor of the unified ESP32
        # connect/status button in the nav bar (see main_window.py,
        # src/ui/status_indicator.py) — this box now only hosts Home.
        conn_box = QGroupBox("CONEXIÓN")
        conn_layout = QHBoxLayout(conn_box)

        self.home_button = QPushButton("Calibrar (Home)")
        self.home_button.setStyleSheet(BUTTON_STYLE)

        conn_layout.addWidget(self.home_button)

        # Fixed height, matched to conn_box's own natural size, so a
        # long status message can never make this box (or the row it
        # shares with conn_box) grow — _AutoFitLabel keeps the text
        # itself (wrapped, shrunk if needed) contained within whatever
        # space that leaves it.
        status_box.setFixedHeight(conn_box.sizeHint().height())

        top_row = QHBoxLayout()
        top_row.setSpacing(LAYOUT_SPACING)
        top_row.addWidget(status_box, 2)
        top_row.addWidget(conn_box, 3)
        root.addLayout(top_row)

        # --- Initial position box ---
        position_box = QGroupBox("POSICIÓN INICIAL")
        position_layout = QGridLayout(position_box)
        position_layout.setSpacing(LAYOUT_SPACING)

        # Row 0: load a previously saved position into the fields below.
        self.saved_positions_combo = QComboBox()
        self.saved_positions_combo.setStyleSheet(INPUT_STYLE)
        self.refresh_positions_button = QPushButton("Actualizar")
        self.refresh_positions_button.setStyleSheet(BUTTON_STYLE_COMPACT)
        self.load_position_button = QPushButton("Cargar")
        self.load_position_button.setStyleSheet(BUTTON_STYLE_COMPACT)

        position_layout.addWidget(QLabel("Guardadas:"), 0, 0)
        position_layout.addWidget(self.saved_positions_combo, 0, 1, 1, 3)
        position_layout.addWidget(self.refresh_positions_button, 0, 4)
        position_layout.addWidget(self.load_position_button, 0, 5)

        # Row 1: the position fields themselves (editable directly, or
        # filled in by "Cargar" above, or updated live by manual moves).
        self.pos_x_input = QLineEdit("0")
        self.pos_y_input = QLineEdit("0")
        self.pos_angle_input = QLineEdit("0")
        # These 3 fields are purely numeric (decimal, possibly negative —
        # e.g. calibrated angle bounds), so they get their own dedicated
        # on-screen numeric keypad (see _keypad below) instead of relying
        # on the OS virtual keyboard, whose show/hide integration proved
        # unreliable to get right blind on this touchscreen. The OS
        # keyboard is explicitly disabled for just these 3 fields
        # (WA_InputMethodEnabled) so the two never compete.
        for edit in (self.pos_x_input, self.pos_y_input, self.pos_angle_input):
            edit.setStyleSheet(INPUT_STYLE)
            edit.setValidator(QDoubleValidator(-1e6, 1e6, 4, edit))
            edit.setAttribute(Qt.WA_InputMethodEnabled, False)
            edit.installEventFilter(self)

        position_layout.addWidget(QLabel("X (cm):"), 1, 0)
        position_layout.addWidget(self.pos_x_input, 1, 1)
        position_layout.addWidget(QLabel("Y (cm):"), 1, 2)
        position_layout.addWidget(self.pos_y_input, 1, 3)
        position_layout.addWidget(QLabel("Ángulo (deg):"), 1, 4)
        position_layout.addWidget(self.pos_angle_input, 1, 5)

        # Row 2: act on the fields above. Saving is deliberately not
        # offered here — the initial position is only ever persisted
        # via the post-trajectory prompt in TrajectoryScreen, once it's
        # clear it was actually used for a run.
        self.goto_position_button = QPushButton("Ir a Posición Inicial")
        self.goto_position_button.setStyleSheet(BUTTON_STYLE_COMPACT)
        position_layout.addWidget(self.goto_position_button, 2, 0, 1, 6)

        # Manual movement used to have its own box here ("MOVIMIENTO
        # MANUAL") — now the joystick control in TrajectoryScreen (see
        # src/ui/manual_joystick.py), so position_box is the only thing
        # left in this row.
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(LAYOUT_SPACING)
        bottom_row.addWidget(position_box)
        root.addLayout(bottom_row)

        root.addStretch()

    def eventFilter(self, watched, event):
        """
        Shows the numeric keypad when one of the 3 initial-position
        fields gains focus (see _build_ui, where they're registered via
        installEventFilter(self)) — a QLineEdit has no focus-in signal
        of its own in Qt, so an event filter is the standard way to
        observe it without subclassing the widget.
        """
        if event.type() == QEvent.FocusIn and watched in (
            self.pos_x_input, self.pos_y_input, self.pos_angle_input,
        ):
            self._keypad.show_for(watched)
        return super().eventFilter(watched, event)

    def _connect_signals(self):
        self.home_button.clicked.connect(self._on_home_clicked)
        self.refresh_positions_button.clicked.connect(self._refresh_position_list)
        self.load_position_button.clicked.connect(self._on_load_position_clicked)
        self.goto_position_button.clicked.connect(self._on_goto_position_clicked)

        self._bridge.state_changed.connect(self._on_state_changed)
        self._bridge.connected.connect(self._on_connected)
        self._bridge.device_error.connect(self._on_device_error)
        self._bridge.disconnected.connect(self._on_disconnected)
        self._bridge.trajectory_finished.connect(self._on_trajectory_finished)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_connected(self):
        """
        The actual connect() call now happens in the nav bar's unified
        ESP32 button (src/ui/status_indicator.py) rather than a button
        on this screen — this reacts to the same bridge.connected signal
        that button's click fires, so this screen's own status text and
        control-enabled state still update exactly as before (same text,
        same _refresh_controls() call the old local click handler made).
        """
        self.status_label.setText("Conectado (sin calibrar)")
        self._refresh_controls()

    def _on_home_clicked(self):
        self._run_action(
            self._bridge.state_machine.home,
            success_message="Calibración completada.",
            on_success=self._prompt_initial_position_setup,
        )

    def _prompt_initial_position_setup(self):
        """
        Right after a successful HOME, ask how to establish the initial
        position for the upcoming trajectory: load a saved one, or type
        a new one. Any position tracked from before this calibration is
        no longer meaningful, so the session resets first.
        """
        self._position_session.reset()

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("Posición Inicial")
        box.setText(
            "Calibración completada. ¿Cómo deseas establecer la posición "
            "inicial de la trayectoria?"
        )
        load_btn = box.addButton("Cargar Guardada", QMessageBox.AcceptRole)
        manual_btn = box.addButton("Ingresar Manualmente", QMessageBox.ActionRole)
        box.addButton("Ahora No", QMessageBox.RejectRole)
        box.exec()

        clicked = box.clickedButton()
        if clicked is load_btn:
            self._prompt_load_saved_position()
        elif clicked is manual_btn:
            self.pos_x_input.setFocus()
            self.status_label.setText(
                "Ingresa la posición inicial y presiona 'Ir a Posición Inicial'."
            )

    def _prompt_load_saved_position(self):
        names = sorted(position_library.list_positions())
        if not names:
            QMessageBox.information(
                self,
                "Sin posiciones guardadas",
                "Todavía no hay posiciones guardadas. Ingresa los valores "
                "manualmente y guarda una nueva cuando quieras.",
            )
            return
        name, ok = QInputDialog.getItem(
            self, "Cargar Posición", "Selecciona una posición guardada:",
            names, editable=False,
        )
        if ok and name:
            self._load_position_by_name(name)

    def _refresh_position_list(self):
        self.saved_positions_combo.clear()
        names = position_library.list_positions()
        if not names:
            self.saved_positions_combo.addItem("(sin posiciones guardadas)")
            self.saved_positions_combo.setEnabled(False)
        else:
            self.saved_positions_combo.setEnabled(True)
            self.saved_positions_combo.addItems(sorted(names))

    def _on_load_position_clicked(self):
        name = self.saved_positions_combo.currentText()
        if not name or name.startswith("("):
            self.status_label.setText("No hay posición guardada seleccionada.")
            return
        self._load_position_by_name(name)

    def _load_position_by_name(self, name: str):
        try:
            position = position_library.load_position(name)
        except Exception as exc:
            self.status_label.setText(f"No se pudo cargar '{name}': {exc}")
            return
        self.pos_x_input.setText(str(position.x))
        self.pos_y_input.setText(str(position.y))
        self.pos_angle_input.setText(str(position.angle))
        # Associates this position with the file it came from right away
        # (not just once GOTO is pressed) — the single source of truth
        # for "which saved file is this", read later by TrajectoryScreen's
        # post-run save prompt.
        self._position_session.saved_name = name
        self.status_label.setText(f"Posición '{name}' cargada. Revisa y presiona 'Ir a Posición Inicial'.")

    def _on_goto_position_clicked(self):
        try:
            x = float(self.pos_x_input.text())
            y = float(self.pos_y_input.text())
            angle = float(self.pos_angle_input.text())
        except ValueError:
            self.status_label.setText("Posición inválida.")
            return
        position = Position(x=x, y=y, angle=angle)

        # A previous trial's initial position is on record (this isn't
        # the first GOTO of the session, e.g. the operator came here via
        # TrajectoryScreen's "Elegir Otro Ensayo") — must not let Y dip
        # below it while getting there, since a prosthesis may be
        # mounted at that height. See
        # SystemStateMachine.safe_return_to_position() for the movement
        # sequence and the safety rule. The very first GOTO after HOME
        # has no such reference yet: instead of an instantaneous jump
        # from the (0,0,0) origin, it uses a synchronized multi-axis
        # trajectory (see _on_goto_initial_synchronized).
        previous = self._position_session.position
        if previous is not None:
            # Pre-check `position` itself (the return trajectory's only
            # validated target — see generate_safe_return_trajectory)
            # so an out-of-range value shows the visual dialog instead
            # of running the background worker just to hit the same
            # rejection as a plain string.
            space = self._bridge.state_machine.last_calibration_space
            if space is not None:
                violations = check_position(position, space)
                if violations:
                    LimitViolationDialog(
                        space, "No se puede establecer esa posición inicial",
                        position, violations, parent=self,
                    ).exec()
                    return

            def action_fn():
                try:
                    self._bridge.state_machine.safe_return_to_position(
                        position, previous.y
                    )
                except PositionOutOfRangeError as exc:
                    raise RuntimeError(
                        f"No se puede establecer esa posición inicial: {exc}"
                    ) from exc

            self._run_action(
                action_fn,
                success_message=f"En posición inicial: X={x:g} cm, Y={y:g} cm, Á={angle:g}°.",
                on_success=lambda: self._on_goto_succeeded(position),
            )
            # Switch to the monitor screen as soon as the move is under
            # way (not once it completes) — that screen's position
            # polling is what actually shows safe_return_to_position()'s
            # multi-step sequence happening live; waiting for on_success
            # here would mean navigating there only after there's
            # nothing left to watch.
            self.request_show_monitor.emit()
        else:
            self._on_goto_initial_synchronized(position)

    def _on_goto_initial_synchronized(self, position: Position):
        """
        First move after HOME: reach `position` from the (0,0,0) origin
        via a synchronized multi-axis trajectory (see
        src/utils/trajectory_generator.py) instead of an instantaneous
        GOTO — all 3 axes start and arrive together. Sent and executed
        through the same TRAJ_BEGIN/TRAJ_POINT/TRAJ_END + RUN protocol
        used for gait trajectories (see docs/protocol.md), so it gets
        live progress (TRAJ_PROGRESS, visualized in
        calibration_map_window.py) and pause/resume/abort for free —
        no new wire protocol or firmware changes needed.
        """
        sm = self._bridge.state_machine
        space = sm.last_calibration_space
        if space is None:
            self.status_label.setText(
                "No hay datos de calibración disponibles; no se puede "
                "validar la posición inicial."
            )
            return

        try:
            points = trajectory_generator.generate_synchronized_trajectory(
                position, space
            )
        except trajectory_generator.PositionOutOfRangeError as exc:
            LimitViolationDialog.from_position_error(
                space, "No se puede establecer esa posición inicial",
                position, exc, parent=self,
            ).exec()
            return

        if not points:
            # Target indistinguishable from the HOME origin: nothing to move.
            self._on_goto_succeeded(position)
            return

        def send_and_run():
            try:
                result = sm.send_trajectory(points)
                if not result.success:
                    raise RuntimeError(
                        f"Transferencia fallida después de "
                        f"{result.points_acknowledged} puntos: {result.error}"
                    )
                sm.run()
            except Exception:
                self._awaiting_initial_move = False
                raise

        self._awaiting_initial_move = True
        self._pending_initial_position = position
        self._run_action(
            send_and_run,
            success_message="Moviendo a la posición inicial (trayectoria sincronizada)...",
            # Fires once RUN is confirmed (RUNNING), not once the move
            # finishes — navigate to the monitor screen right as the
            # movement starts so its live TRAJ_PROGRESS marker is
            # visible for the whole trip, same reasoning as the
            # safe_return_to_position branch above.
            on_success=lambda: self.request_show_monitor.emit(),
        )

    def _on_goto_succeeded(self, position: Position):
        """
        Establishes the initial position in the shared session — this is
        what the manual-move buttons are gated on (see _refresh_controls),
        since a nudge before this point would physically move the device
        from an undefined reference point with no way to reflect it in
        the UI. saved_name is untouched here: it's set once when a saved
        file is loaded (or once a save happens) and GOTO may be pressed
        more than once (e.g. after hand-editing the fields) without
        changing which file the position is associated with.
        """
        self._position_session.set(position)
        self._refresh_controls()

    def _run_action(self, action_fn, success_message: str = "OK", on_success=None):
        """Run a blocking state-machine action on a background thread
        so the UI does not freeze while waiting for the ESP32."""
        self._worker = ActionWorker(action_fn)

        def handle_success():
            if success_message is not None:
                self._on_action_succeeded(success_message)
            if on_success is not None:
                on_success()

        self._worker.succeeded.connect(handle_success)
        self._worker.failed.connect(self._on_action_failed)
        self._worker.start()

    def _on_action_succeeded(self, message: str):
        self.status_label.setText(message)

    def _on_action_failed(self, message: str):
        self.status_label.setText(f"Acción fallida: {message}")

    # ------------------------------------------------------------------
    # Reacting to state changes (safe: runs on the main/UI thread,
    # thanks to StateMachineBridge)
    # ------------------------------------------------------------------

    def _on_state_changed(self, state_name: str):
        # HOME no longer streams per-axis progress (see docs/protocol.md,
        # "Cambio 2026-08-31 (READY con límites)") — a single generic
        # message covers the whole sweep, from HOMING until READY/ERROR
        # resolves it.
        if state_name == "HOMING":
            self.status_label.setText("Calibrando...")
        else:
            self.status_label.setText(state_name)
        self._refresh_controls()

    def _on_device_error(self, code: str, message: str):
        self.status_label.setText(f"Error del ESP32 [{code}]: {message}")
        # An error during the synchronized initial move (e.g. a limit
        # reached mid-trajectory) falls back to IDLE without ever
        # reporting FINISHED (see SystemStateMachine._on_device_error) —
        # clear the guard so a later, unrelated trajectory_finished isn't
        # mistaken for this one having actually completed.
        self._awaiting_initial_move = False

    def _on_disconnected(self):
        self.status_label.setText("DISCONNECTED")
        self._awaiting_initial_move = False
        self._refresh_controls()

    def _on_trajectory_finished(self):
        """
        Reacts only to the synchronized initial-position move started by
        _on_goto_initial_synchronized — a gait trajectory finishing over
        on TrajectoryScreen fires this same shared bridge signal but
        must NOT be mistaken for this screen's own move (see
        _awaiting_initial_move).
        """
        if not self._awaiting_initial_move:
            return
        self._awaiting_initial_move = False
        position = self._pending_initial_position
        self._pending_initial_position = None
        if position is None:
            return
        self.status_label.setText(
            f"En posición inicial: X={position.x:g} cm, Y={position.y:g} cm, "
            f"Á={position.angle:g}°."
        )
        # Navigation already happened in _on_goto_initial_synchronized's
        # on_success (right as RUN was confirmed) — this only records the
        # final position now that the move has actually completed.
        self._on_goto_succeeded(position)

    def _refresh_controls(self):
        """Enable/disable buttons based on what the state machine
        currently allows — single source of truth, no duplicated logic."""
        sm = self._bridge.state_machine
        controller = sm.controller

        self.home_button.setEnabled(controller.is_connected and sm.can_home())

        can_goto = controller.is_connected and sm.can_go_to_position()
        self.goto_position_button.setEnabled(can_goto)

    def showEvent(self, event):
        """
        Refreshes the 3 position fields from the shared session whenever
        this screen becomes visible — needed since manual moves now
        happen from TrajectoryScreen's joystick control (see
        src/ui/manual_joystick.py), which updates position_session
        directly but has no way to reach these fields itself. Mirrors
        what the old local _on_manual_move_succeeded used to do inline,
        just triggered by visibility instead of by the move handler
        that no longer lives on this screen.
        """
        super().showEvent(event)
        position = self._position_session.position
        if position is not None:
            self.pos_x_input.setText(f"{position.x:g}")
            self.pos_y_input.setText(f"{position.y:g}")
            self.pos_angle_input.setText(f"{position.angle:g}")
