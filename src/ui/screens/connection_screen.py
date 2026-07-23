"""
connection_screen.py

First screen: serial connection, homing, initial-position setup, and
manual per-axis movement. Follows the pattern used throughout the
project: this screen never talks to ESP32Controller directly — it goes
through SystemStateMachine (via StateMachineBridge) so permission logic
(can_move_manually(), can_home(), etc.) stays centralized in one place.

Initial-position bookkeeping (which saved file, if any, the current
fields correspond to) is written to a shared InitialPositionSession so
TrajectoryScreen can apply it as an offset and, once a run finishes,
offer to save it — see src/controllers/initial_position_session.py.
"""

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QLabel, QLineEdit, QGroupBox, QComboBox, QButtonGroup,
    QInputDialog, QMessageBox
)

from src.communication.protocol import Position
from src.controllers.initial_position_session import InitialPositionSession
from src.ui.bridge import StateMachineBridge
from src.ui.style import (
    BUTTON_STYLE, BUTTON_STYLE_PRIMARY, BUTTON_STYLE_COMPACT,
    LAYOUT_SPACING, LAYOUT_MARGIN, FONT_SIZE_STATUS, FONT_SIZE_NORMAL
)
from src.utils import position_library

# Fixed manual-movement increments (cm for X/Y, degrees for A — the
# same magnitudes read naturally in both units). Replaces a free-text
# amount field: a small, professional-looking preset selector is less
# error-prone on a touchscreen than typing an arbitrary number each time.
MOVE_INCREMENTS = (1.0, 5.0, 10.0)

# Spanish labels for calibration status messages, keyed by the axis
# codes used throughout the protocol (Y/X/A — see docs/protocol.md).
# The resulting movement-space MAP is drawn in a separate window (see
# calibration_map_window.py) — this screen only shows live text status
# while HOMING is in progress.
_AXIS_LABELS_ES = {"Y": "Y (vertical)", "X": "X (horizontal)", "A": "ángulo"}


class _ActionWorker(QThread):
    """
    Runs a single blocking state-machine action (home, move_relative,
    etc.) on a background thread, so the UI stays responsive while
    waiting for the ESP32's response/timeout.

    This is intentionally minimal — a single-shot worker per action,
    not a persistent command queue. If future screens need more
    sophisticated sequencing (e.g. trajectory transfer with progress),
    that will be designed separately rather than overloading this class.
    """
    succeeded = Signal()
    failed = Signal(str)

    def __init__(self, action_fn, parent=None):
        super().__init__(parent)
        self._action_fn = action_fn

    def run(self):
        try:
            self._action_fn()
            self.succeeded.emit()
        except Exception as exc:
            self.failed.emit(str(exc))


class ConnectionScreen(QWidget):
    """
    Screen for establishing the serial connection, homing, setting up
    the initial position, and manual per-axis movement.

    Args:
        bridge: The shared StateMachineBridge instance (created once
                in main_window.py and passed to every screen).
        position_session: The shared InitialPositionSession (also
                owned by main_window.py), so TrajectoryScreen can see
                what this screen sets up.
    """

    def __init__(
        self,
        bridge: StateMachineBridge,
        position_session: InitialPositionSession,
        parent=None,
    ):
        super().__init__(parent)
        self._bridge = bridge
        self._position_session = position_session
        self._worker = None  # keeps a reference so the QThread isn't GC'd mid-run

        # Currently selected manual-move increment (cm/deg).
        self._move_increment = MOVE_INCREMENTS[0]

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

        self.status_label = QLabel("DISCONNECTED")
        self.status_label.setStyleSheet(
            f"font-size: {FONT_SIZE_STATUS}px; font-weight: bold;"
        )
        status_layout.addWidget(self.status_label)

        # --- Connection / Home box ---
        conn_box = QGroupBox("CONEXIÓN")
        conn_layout = QHBoxLayout(conn_box)

        self.connect_button = QPushButton("Conectar")
        self.connect_button.setStyleSheet(BUTTON_STYLE_PRIMARY)
        self.home_button = QPushButton("Calibrar (Home)")
        self.home_button.setStyleSheet(BUTTON_STYLE)

        conn_layout.addWidget(self.connect_button)
        conn_layout.addWidget(self.home_button)

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
        self.saved_positions_combo.setStyleSheet(
            f"font-size: {FONT_SIZE_NORMAL}px; min-height: 44px;"
        )
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
        for edit in (self.pos_x_input, self.pos_y_input, self.pos_angle_input):
            edit.setStyleSheet(f"font-size: {FONT_SIZE_NORMAL}px; min-height: 44px;")

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

        # --- Manual movement grid ---
        manual_box = QGroupBox("MOVIMIENTO MANUAL")
        manual_layout = QGridLayout(manual_box)
        manual_layout.setSpacing(LAYOUT_SPACING)

        # Row 0: increment selector (1 / 5 / 10 cm or deg), shared by
        # every axis — a small preset picker instead of a free-text
        # amount, less error-prone on a touchscreen.
        manual_layout.addWidget(QLabel("Incremento:"), 0, 0)
        increments_row = QHBoxLayout()
        self._increment_group = QButtonGroup(self)
        self._increment_group.setExclusive(True)
        for value in MOVE_INCREMENTS:
            btn = QPushButton(f"{value:g}")
            btn.setCheckable(True)
            btn.setStyleSheet(self._increment_button_style())
            btn.setChecked(value == self._move_increment)
            btn.clicked.connect(lambda checked, v=value: self._on_increment_selected(v))
            self._increment_group.addButton(btn)
            increments_row.addWidget(btn)
        manual_layout.addLayout(increments_row, 0, 1, 1, 2)

        self.axis_buttons = {}  # (axis, direction) -> QPushButton
        axes = [("X", "Horizontal"), ("Y", "Vertical"), ("A", "Sagital")]
        for row, (axis_code, axis_label) in enumerate(axes, start=1):
            manual_layout.addWidget(QLabel(axis_label), row, 0)

            minus_btn = QPushButton(f"{axis_code} -")
            minus_btn.setStyleSheet(BUTTON_STYLE_COMPACT)
            minus_btn.clicked.connect(
                lambda checked=False, a=axis_code: self._on_manual_move(a, "-")
            )
            manual_layout.addWidget(minus_btn, row, 1)
            self.axis_buttons[(axis_code, "-")] = minus_btn

            plus_btn = QPushButton(f"{axis_code} +")
            plus_btn.setStyleSheet(BUTTON_STYLE_COMPACT)
            plus_btn.clicked.connect(
                lambda checked=False, a=axis_code: self._on_manual_move(a, "+")
            )
            manual_layout.addWidget(plus_btn, row, 2)
            self.axis_buttons[(axis_code, "+")] = plus_btn

        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(LAYOUT_SPACING)
        bottom_row.addWidget(position_box, 3)
        bottom_row.addWidget(manual_box, 2)
        root.addLayout(bottom_row)

        root.addStretch()

    @staticmethod
    def _increment_button_style() -> str:
        """Same compact footprint as BUTTON_STYLE_COMPACT, plus a
        visibly highlighted checked state (radio-button behavior)."""
        return BUTTON_STYLE_COMPACT + """
            QPushButton:checked {
                background-color: #3987e5;
                color: white;
                font-weight: bold;
            }
        """

    def _connect_signals(self):
        self.connect_button.clicked.connect(self._on_connect_clicked)
        self.home_button.clicked.connect(self._on_home_clicked)
        self.refresh_positions_button.clicked.connect(self._refresh_position_list)
        self.load_position_button.clicked.connect(self._on_load_position_clicked)
        self.goto_position_button.clicked.connect(self._on_goto_position_clicked)

        self._bridge.state_changed.connect(self._on_state_changed)
        self._bridge.device_error.connect(self._on_device_error)
        self._bridge.disconnected.connect(self._on_disconnected)
        self._bridge.calibration_limit.connect(self._on_calibration_limit)
        self._bridge.calibration_progress.connect(self._on_calibration_progress)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_connect_clicked(self):
        controller = self._bridge.state_machine.controller
        if not controller.is_connected:
            controller.connect()
            self.status_label.setText("Conectado (sin calibrar)")
            self._bridge.notify_connected()
        self._refresh_controls()

    def _on_home_clicked(self):
        self._run_action(
            self._bridge.state_machine.home,
            success_message="Calibración completada.",
            on_success=self._prompt_initial_position_setup,
        )

    def _on_calibration_limit(self, axis: str, bound: str, value):
        label = _AXIS_LABELS_ES.get(axis, axis)
        which = "mínimo" if bound == "MIN" else "máximo"
        suffix = f" ({value:.1f})" if value is not None else ""
        self.status_label.setText(
            f"Calibrando eje {label}: límite {which} alcanzado{suffix}."
        )

    def _on_calibration_progress(self, axis: str, value: float):
        label = _AXIS_LABELS_ES.get(axis, axis)
        self.status_label.setText(f"Calibrando eje {label}... {value:.1f}")

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
        # has no such reference yet, so it stays a plain GOTO.
        previous = self._position_session.position
        if previous is not None:
            action_fn = lambda: self._bridge.state_machine.safe_return_to_position(
                position, previous.y
            )
        else:
            action_fn = lambda: self._bridge.state_machine.go_to_position(position)

        self._run_action(
            action_fn,
            success_message=f"En posición inicial: X={x:g} cm, Y={y:g} cm, Á={angle:g}°.",
            on_success=lambda: self._on_goto_succeeded(position),
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

    def _on_increment_selected(self, value: float):
        self._move_increment = value

    def _on_manual_move(self, axis: str, direction: str):
        amount = self._move_increment
        # No success_message here: the operator should see the resulting
        # X/Y/Angle values change directly in the Posición Inicial fields
        # (via _on_manual_move_succeeded below), not a "+5"/"-5" delta
        # message in Estado del Sistema.
        self._run_action(
            lambda: self._bridge.state_machine.move_relative(axis, direction, amount),
            success_message=None,
            on_success=lambda: self._on_manual_move_succeeded(axis, direction, amount),
        )

    def _on_manual_move_succeeded(self, axis: str, direction: str, amount: float):
        """
        Mirrors the just-applied delta into the session and the visible
        fields in real time. MOVE_REL amounts are already in real units
        (unlike step-based MANUAL), so this client-side accumulation
        exactly mirrors the firmware's own tracked position — no
        GET_POSITION round trip needed on every tap.
        """
        self._position_session.apply_manual_delta(axis, direction, amount)
        position = self._position_session.position
        if position is not None:
            self.pos_x_input.setText(f"{position.x:g}")
            self.pos_y_input.setText(f"{position.y:g}")
            self.pos_angle_input.setText(f"{position.angle:g}")

    def _run_action(self, action_fn, success_message: str = "OK", on_success=None):
        """Run a blocking state-machine action on a background thread
        so the UI does not freeze while waiting for the ESP32.

        success_message may be None to skip updating the status label
        entirely (used by manual moves, where the position fields
        themselves are the intended feedback, not a status message)."""
        self._worker = _ActionWorker(action_fn)

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
        self.status_label.setText(state_name)
        self._refresh_controls()

    def _on_device_error(self, code: str, message: str):
        self.status_label.setText(f"Error del ESP32 [{code}]: {message}")

    def _on_disconnected(self):
        self.status_label.setText("DISCONNECTED")
        self._refresh_controls()

    def _refresh_controls(self):
        """Enable/disable buttons based on what the state machine
        currently allows — single source of truth, no duplicated logic."""
        sm = self._bridge.state_machine
        controller = sm.controller

        self.connect_button.setEnabled(not controller.is_connected)
        self.home_button.setEnabled(controller.is_connected and sm.can_home())

        can_goto = controller.is_connected and sm.can_go_to_position()
        self.goto_position_button.setEnabled(can_goto)

        # Also requires an initial position to already be established
        # (via "Ir a Posición Inicial") — nudging before that would move
        # the device from an undefined reference point with no way to
        # reflect the result in the UI (see _on_goto_succeeded).
        can_move = sm.can_move_manually() and self._position_session.position is not None
        for button in self.axis_buttons.values():
            button.setEnabled(can_move)
            button.setToolTip(
                "" if can_move else
                "Primero presiona 'Ir a Posición Inicial'."
            )
