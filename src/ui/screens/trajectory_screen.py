"""
trajectory_screen.py

Screen for selecting, loading, sending, and running a gait trajectory.
Follows the same pattern as ConnectionScreen: all actions go through
SystemStateMachine (via the shared StateMachineBridge), never directly
through ESP32Controller, so permission logic stays centralized.
"""

import time

import pyqtgraph as pg
from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QComboBox, QGroupBox, QInputDialog, QMessageBox
)

from src.communication.protocol import TrajectoryPoint
from src.controllers.initial_position_session import InitialPositionSession
from src.ui.bridge import StateMachineBridge
from src.ui.style import (
    BUTTON_STYLE, BUTTON_STYLE_PRIMARY, BUTTON_STYLE_COMPACT,
    LAYOUT_SPACING, LAYOUT_MARGIN, FONT_SIZE_NORMAL,
    COLOR_AXIS_X, COLOR_AXIS_Y, COLOR_AXIS_ANGLE,
)
from src.utils import position_library
from src.utils.trajectory_library import list_trajectories, load_trajectory_by_id


class _ActionWorker(QThread):
    """
    Same minimal single-shot background worker pattern used in
    ConnectionScreen, duplicated here rather than shared for now since
    it is a small, self-contained class — if a third screen needs it,
    this is the signal to promote it to a shared module under src/ui/.
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


class TrajectoryScreen(QWidget):
    """
    Screen for selecting a trajectory, sending it to the ESP32, and
    controlling its execution (Run / Pause / Resume).

    Args:
        bridge: The shared StateMachineBridge instance.
        position_session: The shared InitialPositionSession set up in
                ConnectionScreen — used to offset trajectory points onto
                the chosen initial position, and to offer saving it once
                a run finishes (see src/controllers/initial_position_session.py).
    """

    # Emitted when the operator picks "Elegir Otro Ensayo" — main_window.py
    # connects this to switching to ConnectionScreen (screens otherwise
    # know nothing of each other, per the project's screen-decoupling rule;
    # MainWindow is the one place cross-screen navigation is wired).
    request_new_trial = Signal()

    def __init__(
        self,
        bridge: StateMachineBridge,
        position_session: InitialPositionSession,
        parent=None,
    ):
        super().__init__(parent)
        self._bridge = bridge
        self._position_session = position_session
        self._worker = None
        self._loaded_points = None  # List[TrajectoryPoint] once loaded

        # Live-plot data buffers, filled incrementally by trajectory_progress
        # signals while RUNNING; reset on Run and on trajectory_finished.
        self._plot_t = []
        self._plot_x = []
        self._plot_y = []
        self._plot_angle = []

        self._build_ui()
        self._connect_signals()
        self._refresh_trajectory_list()
        self._refresh_controls()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setSpacing(LAYOUT_SPACING)
        root.setContentsMargins(
            LAYOUT_MARGIN, LAYOUT_MARGIN, LAYOUT_MARGIN, LAYOUT_MARGIN
        )

        left = QVBoxLayout()
        left.setSpacing(LAYOUT_SPACING)
        root.addLayout(left, stretch=1)

        self._build_plot_box(root)

        # --- Selection ---
        select_box = QGroupBox("TRAJECTORY SELECTION")
        select_layout = QVBoxLayout(select_box)

        self.trajectory_combo = QComboBox()
        self.trajectory_combo.setStyleSheet(
            f"font-size: {FONT_SIZE_NORMAL}px; min-height: 60px;"
        )
        select_layout.addWidget(self.trajectory_combo)

        row = QHBoxLayout()
        self.refresh_button = QPushButton("Refresh List")
        self.refresh_button.setStyleSheet(BUTTON_STYLE)
        self.send_button = QPushButton("Load && Send")
        self.send_button.setStyleSheet(BUTTON_STYLE)
        row.addWidget(self.refresh_button)
        row.addWidget(self.send_button)
        select_layout.addLayout(row)

        left.addWidget(select_box)

        # --- Execution ---
        exec_box = QGroupBox("EXECUTION")
        exec_box_layout = QVBoxLayout(exec_box)
        exec_box_layout.setSpacing(LAYOUT_SPACING)

        exec_layout = QHBoxLayout()
        self.run_button = QPushButton("Run")
        self.run_button.setStyleSheet(BUTTON_STYLE_PRIMARY)
        self.pause_button = QPushButton("Pause")
        self.pause_button.setStyleSheet(BUTTON_STYLE)
        self.resume_button = QPushButton("Resume")
        self.resume_button.setStyleSheet(BUTTON_STYLE)

        exec_layout.addWidget(self.run_button)
        exec_layout.addWidget(self.pause_button)
        exec_layout.addWidget(self.resume_button)
        exec_box_layout.addLayout(exec_layout)

        # Second row: only enabled while PAUSED — abandon the paused
        # trial (see SystemStateMachine.abort()) instead of resuming it,
        # because the operator observed a mechanical fault. Separate row
        # from Run/Pause/Resume so it doesn't overflow the column width,
        # and visually reads as a distinct, less-frequent action set.
        retry_layout = QHBoxLayout()
        self.restart_trial_button = QPushButton("Reiniciar Ensayo")
        self.restart_trial_button.setStyleSheet(BUTTON_STYLE_COMPACT)
        self.choose_other_button = QPushButton("Elegir Otro Ensayo")
        self.choose_other_button.setStyleSheet(BUTTON_STYLE_COMPACT)
        retry_layout.addWidget(self.restart_trial_button)
        retry_layout.addWidget(self.choose_other_button)
        exec_box_layout.addLayout(retry_layout)

        left.addWidget(exec_box)

        # --- Feedback ---
        self.info_label = QLabel("No trajectory loaded.")
        self.info_label.setStyleSheet(f"font-size: {FONT_SIZE_NORMAL}px;")
        left.addWidget(self.info_label)

        left.addStretch()

    # Shared with monitor_3d_window.py (see style.py's COLOR_AXIS_* constants)
    # so the same axis reads as the same color throughout the app.
    _COLOR_POS_X = COLOR_AXIS_X
    _COLOR_POS_Y = COLOR_AXIS_Y
    _COLOR_ANGLE = COLOR_AXIS_ANGLE

    def _build_plot_box(self, root):
        pg.setConfigOptions(background="#1a1a19", foreground="#ffffff")

        plot_box = QGroupBox("LIVE TRAJECTORY PLOT")
        plot_layout = QVBoxLayout(plot_box)

        self._plot_widget = pg.GraphicsLayoutWidget()

        self._x_plot = self._plot_widget.addPlot(row=0, col=0)
        self._x_plot.setTitle("Posición X vs Tiempo")
        self._x_plot.setLabel("left", "Posición X", units="cm")
        self._x_curve = self._x_plot.plot(pen=pg.mkPen(color=self._COLOR_POS_X, width=2))

        self._y_plot = self._plot_widget.addPlot(row=1, col=0)
        self._y_plot.setTitle("Posición Y vs Tiempo")
        self._y_plot.setLabel("left", "Posición Y", units="cm")
        self._y_curve = self._y_plot.plot(pen=pg.mkPen(color=self._COLOR_POS_Y, width=2))

        self._angle_plot = self._plot_widget.addPlot(row=2, col=0)
        self._angle_plot.setTitle("Ángulo vs Tiempo")
        self._angle_plot.setLabel("left", "Ángulo", units="deg")
        self._angle_plot.setLabel("bottom", "Tiempo", units="s")
        self._angle_curve = self._angle_plot.plot(pen=pg.mkPen(color=self._COLOR_ANGLE, width=2))

        self._y_plot.setXLink(self._x_plot)
        self._angle_plot.setXLink(self._x_plot)

        plot_layout.addWidget(self._plot_widget)

        self.clear_plot_button = QPushButton("Limpiar Gráfica")
        self.clear_plot_button.setStyleSheet(BUTTON_STYLE)
        self.clear_plot_button.clicked.connect(self._clear_plot)
        plot_layout.addWidget(self.clear_plot_button)

        root.addWidget(plot_box, stretch=1)

    def _connect_signals(self):
        self.refresh_button.clicked.connect(self._refresh_trajectory_list)
        self.send_button.clicked.connect(self._on_send_clicked)
        self.run_button.clicked.connect(self._on_run_clicked)
        self.pause_button.clicked.connect(self._on_pause_clicked)
        self.resume_button.clicked.connect(self._on_resume_clicked)
        self.restart_trial_button.clicked.connect(self._on_restart_trial_clicked)
        self.choose_other_button.clicked.connect(self._on_choose_other_clicked)

        self._bridge.state_changed.connect(self._on_state_changed)
        self._bridge.trajectory_finished.connect(self._on_trajectory_finished)
        self._bridge.trajectory_progress.connect(self._on_trajectory_progress)
        self._bridge.device_error.connect(self._on_device_error)
        self._bridge.disconnected.connect(self._on_disconnected)

    # ------------------------------------------------------------------
    # Trajectory list
    # ------------------------------------------------------------------

    def _refresh_trajectory_list(self):
        self.trajectory_combo.clear()
        ids = list_trajectories()
        if not ids:
            self.trajectory_combo.addItem("(no trajectories found)")
            self.trajectory_combo.setEnabled(False)
        else:
            self.trajectory_combo.setEnabled(True)
            self.trajectory_combo.addItems(sorted(ids))

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_send_clicked(self):
        trajectory_id = self.trajectory_combo.currentText()
        if not trajectory_id or trajectory_id.startswith("("):
            self.info_label.setText("No valid trajectory selected.")
            return

        try:
            self._loaded_points = load_trajectory_by_id(trajectory_id)
        except Exception as exc:
            self.info_label.setText(f"Failed to load '{trajectory_id}': {exc}")
            self._loaded_points = None
            return

        self.info_label.setText(
            f"Loaded '{trajectory_id}' ({len(self._loaded_points)} points). Sending..."
        )
        self._run_action(
            lambda: self._send_and_check(),
            success_message=f"'{trajectory_id}' sent and stored. Ready to run."
        )

    def _send_and_check(self):
        """
        Runs on the background thread. send_trajectory() itself does
        not raise on a failed transfer (it returns a result object) —
        we raise here so the worker's failed/succeeded signals reflect
        the real outcome.
        """
        points = self._offset_points(self._loaded_points)
        result = self._bridge.state_machine.send_trajectory(points)
        if not result.success:
            raise RuntimeError(
                f"Transfer failed after {result.points_acknowledged} "
                f"points: {result.error}"
            )

    def _offset_points(self, points):
        """
        Shift every loaded point by the initial position set up in
        ConnectionScreen (GOTO already moved the rig there; TRAJ_POINT
        coordinates are absolute relative to the HOME origin, per
        docs/protocol.md, so the CSV's own origin-relative points must
        be shifted to start from that chosen position). If no initial
        position was set up, points are sent as-is (origin as start).
        """
        position = self._position_session.position
        if position is None:
            return points
        return [
            TrajectoryPoint(
                t=p.t,
                x=p.x + position.x,
                y=p.y + position.y,
                angle=p.angle + position.angle,
            )
            for p in points
        ]

    def _on_run_clicked(self):
        self._clear_plot()
        self._run_action(self._bridge.state_machine.run, success_message="Running...")

    def _on_pause_clicked(self):
        self._run_action(self._bridge.state_machine.pause, success_message="Paused.")

    def _on_resume_clicked(self):
        self._run_action(self._bridge.state_machine.resume, success_message="Resumed.")

    # ------------------------------------------------------------------
    # Retry flow: after FINISHED (declined the save-position offer) or
    # from PAUSED (operator observed a mechanical fault). Two choices:
    # restart the SAME trial in place (reposition + resend + run,
    # automatically), or abandon it and go set up a different one via
    # ConnectionScreen's existing initial-position flow. See
    # SystemStateMachine.safe_return_to_position() for the movement
    # sequence and the safety rule behind it.
    # ------------------------------------------------------------------

    def _on_restart_trial_clicked(self):
        """
        Restarts the SAME trial: reposition to the SAME initial point
        used last, then resend the SAME loaded trajectory and run it —
        no manual "Load && Send"/"Run" presses needed. Resending (rather
        than relying on the ESP32 still having it stored from before)
        keeps this independent of how long TRAJ_STORED persists across
        an ABORT on whatever firmware is running.
        """
        sm = self._bridge.state_machine
        was_paused = sm.can_abort()

        target = self._position_session.position
        if target is None:
            self.info_label.setText("No hay una posición inicial previa registrada.")
            return
        if self._loaded_points is None:
            self.info_label.setText("No hay una trayectoria cargada para reiniciar.")
            return
        floor_y = target.y

        def do_restart():
            if was_paused:
                sm.abort()
            sm.safe_return_to_position(target, floor_y)
            self._send_and_check()
            sm.run()

        self._clear_plot()
        self.info_label.setText("Regresando a la posición inicial...")
        self._run_action(
            do_restart,
            success_message="Ensayo reiniciado. Running...",
            min_duration_ms=3000,
        )

    def _on_choose_other_clicked(self):
        """
        Abandons the current trial (if paused) and hands off to
        ConnectionScreen to set up a different one — same screen, same
        "Ir a Posición Inicial" flow used the first time, which now
        also applies the safe repositioning sequence whenever a
        previous trial's position is on record (see
        ConnectionScreen._on_goto_position_clicked). Reuses that
        existing flow instead of duplicating position-entry UI here.
        """
        sm = self._bridge.state_machine
        if sm.can_abort():
            self._run_action(
                sm.abort,
                success_message="Ensayo abandonado.",
                on_success=self.request_new_trial.emit,
            )
        else:
            self.request_new_trial.emit()

    def _run_action(
        self, action_fn, success_message: str = "OK", on_success=None,
        min_duration_ms: int = 0,
    ):
        """
        min_duration_ms: keeps whatever the info_label already shows
        (set by the caller right before this) visible for at least this
        long before switching to success_message — without this, a fast
        action (e.g. the test firmware's near-instantaneous simulated
        GOTO) can flip the label before the operator has time to read
        it. Only pads the display, never delays a slow real action
        beyond however long it actually takes.
        """
        self._worker = _ActionWorker(action_fn)
        start_time = time.monotonic()

        def apply_success():
            if success_message is not None:
                self._on_action_succeeded(success_message)
            if on_success is not None:
                on_success()

        def handle_success():
            elapsed_ms = (time.monotonic() - start_time) * 1000
            remaining_ms = min_duration_ms - elapsed_ms
            if remaining_ms > 0:
                QTimer.singleShot(int(remaining_ms), apply_success)
            else:
                apply_success()

        self._worker.succeeded.connect(handle_success)
        self._worker.failed.connect(self._on_action_failed)
        self._worker.start()

    def _on_action_succeeded(self, message: str):
        self.info_label.setText(message)
        self._refresh_controls()

    def _on_action_failed(self, message: str):
        self.info_label.setText(f"Failed: {message}")
        self._refresh_controls()

    # ------------------------------------------------------------------
    # Reacting to state changes
    # ------------------------------------------------------------------

    def _on_state_changed(self, state_name: str):
        self._refresh_controls()

    def _on_trajectory_finished(self):
        self.info_label.setText("Trajectory FINISHED.")
        self._refresh_controls()
        self._maybe_offer_save_position()

    def _maybe_offer_save_position(self):
        """
        Offer to save the initial position used for this run:
        - Loaded from a saved file and still matching what's on disk:
          nothing to save, skip silently.
        - Loaded from a saved file but different from what's on disk
          (edited since, via nudges or re-typed values + GOTO): offer
          to update that same file with the latest values.
        - Entered manually (never associated with a saved file): offer
          to save it as a brand-new position.

        Whether it "still matches" is checked directly against the
        saved file's actual contents, not a manually-tracked dirty
        flag — see InitialPositionSession's docstring for why.
        """
        session = self._position_session
        if session.position is None:
            return

        if session.saved_name is not None:
            if self._matches_saved_file(session):
                return
            self._offer_update_saved_position(session)
        else:
            self._offer_save_as_new_position(session)

    @staticmethod
    def _matches_saved_file(session: InitialPositionSession, tolerance: float = 1e-6) -> bool:
        try:
            saved = position_library.load_position(session.saved_name)
        except Exception:
            return False  # file missing/unreadable: treat as changed, offer to (re)save
        p = session.position
        return (
            abs(p.x - saved.x) < tolerance
            and abs(p.y - saved.y) < tolerance
            and abs(p.angle - saved.angle) < tolerance
        )

    def _offer_update_saved_position(self, session: InitialPositionSession):
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("Guardar Posición Inicial")
        box.setText(
            f"La posición inicial usada en esta trayectoria fue "
            f"modificada desde que se cargó '{session.saved_name}'. "
            f"¿Deseas actualizar esa posición guardada con los últimos "
            f"valores?"
        )
        update_btn = box.addButton("Actualizar", QMessageBox.AcceptRole)
        box.addButton("No Guardar", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is update_btn:
            position_library.save_position(session.saved_name, session.position)
        # Declined: nothing else to do here — "Reiniciar Ensayo"/"Elegir
        # Otro Ensayo" are always available once FINISHED regardless of
        # what happens with this save prompt (see _refresh_controls),
        # not gated behind this dialog anymore.

    def _offer_save_as_new_position(self, session: InitialPositionSession):
        name, ok = QInputDialog.getText(
            self, "Guardar Posición Inicial",
            "Esta posición inicial fue ingresada manualmente. "
            "Nombre para guardarla (vacío para omitir):",
        )
        if not ok or not name.strip():
            return  # declined — same reasoning as above, nothing else to do
        name = name.strip()
        position_library.save_position(name, session.position)
        session.saved_name = name

    def _on_trajectory_progress(self, t: float, x: float, y: float, angle: float):
        self._plot_t.append(t)
        self._plot_x.append(x)
        self._plot_y.append(y)
        self._plot_angle.append(angle)
        self._x_curve.setData(self._plot_t, self._plot_x)
        self._y_curve.setData(self._plot_t, self._plot_y)
        self._angle_curve.setData(self._plot_t, self._plot_angle)

    def _clear_plot(self):
        self._plot_t = []
        self._plot_x = []
        self._plot_y = []
        self._plot_angle = []
        self._x_curve.setData([], [])
        self._y_curve.setData([], [])
        self._angle_curve.setData([], [])

    def _on_device_error(self, code: str, message: str):
        self.info_label.setText(f"ERROR [{code}]: {message}")

    def _on_disconnected(self):
        self.info_label.setText("Disconnected.")
        self._refresh_controls()

    def _refresh_controls(self):
        sm = self._bridge.state_machine

        self.send_button.setEnabled(sm.can_send_trajectory())
        self.run_button.setEnabled(sm.can_run())
        self.pause_button.setEnabled(sm.can_pause())
        self.resume_button.setEnabled(sm.can_resume())

        # Available both while PAUSED (fault observed mid-run) and while
        # IDLE after a trial FINISHED — NOT the very first IDLE right
        # after HOME, before anything has run yet, hence the extra
        # _loaded_points/position checks. can_run() covers "IDLE" without
        # importing SystemState here (screens use the state machine's
        # query methods, not the enum, per the project's layering rule).
        can_retry = (
            (sm.can_abort() or sm.can_run())
            and self._loaded_points is not None
            and self._position_session.position is not None
        )
        self.restart_trial_button.setEnabled(can_retry)
        self.choose_other_button.setEnabled(can_retry)