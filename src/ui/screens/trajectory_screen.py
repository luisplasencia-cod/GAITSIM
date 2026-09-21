"""
trajectory_screen.py

Screen for selecting, loading, sending, and running a gait trajectory.
Follows the same pattern as ConnectionScreen: all actions go through
SystemStateMachine (via the shared StateMachineBridge), never directly
through ESP32Controller, so permission logic stays centralized.

Consolidated 2026-07-25 (Luis's request) to also be this app's single
main working view: the position/calibration monitor formerly known as
"Monitor Posición" (its own top-level window, monitor_3d_window.py) is
now this screen's dominant central element (see _build_platform_panel),
with the trajectory selection/execution controls and the live plot
demoted to a secondary sidebar. No behavior changed for any of the
three — same widgets, same handlers, same validation/execution/plot-
isolation logic — only WHERE they live changed. See platform_view.py
(renamed from monitor_3d_window.py) for the PlatformView/PositionPoller
classes reused here as-is.
"""

import time

import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QComboBox, QGroupBox, QInputDialog, QMessageBox,
    QDoubleSpinBox
)

from src.communication.protocol import Position, TrajectoryPoint
from src.controllers import tara_library
from src.controllers.initial_position_session import InitialPositionSession
from src.ui.action_worker import ActionWorker as _ActionWorker
from src.ui.bridge import StateMachineBridge
from src.ui.device_error_dialog import show_device_error
from src.ui.limit_violation_dialog import LimitViolationDialog
from src.ui.manual_joystick import ManualJoystickControl
from src.ui.platform_view import PlatformView, PositionPoller
from src.ui.theme_manager import ThemeManager
from src.ui.style import (
    BUTTON_STYLE_SLIM, BUTTON_STYLE_PRIMARY_SLIM,
    LAYOUT_SPACING, LAYOUT_MARGIN, FONT_SIZE_NORMAL, FONT_SIZE_LARGE,
    COLOR_AXIS_X, COLOR_AXIS_Y, COLOR_AXIS_ANGLE,
    COLOR_BG, COLOR_TEXT, COLOR_TEXT_MUTED, COLOR_DANGER,
)
from src.utils import position_library, trajectory_generator
from src.utils.trajectory_library import list_trajectories, load_trajectory_by_id
from src.controllers.system_state import TrajectorySpeedExceededError
from src.utils.trajectory_validator import TrajectoryOutOfRangeError, validate_trajectory


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
        theme_manager: ThemeManager,
        parent=None,
    ):
        super().__init__(parent)
        self._bridge = bridge
        self._position_session = position_session
        self._theme_manager = theme_manager
        self._worker = None
        # PlatformView's polling companion (see platform_view.py) —
        # created lazily on showEvent, stopped on hideEvent, exactly
        # the lifecycle the old standalone Monitor3DWindow used, just
        # driven by THIS screen's own show/hide now (QStackedWidget
        # still fires those when main_window.py switches pages).
        self._poller = None
        # The ensayo's own data, loaded from CSV — origin-relative
        # List[TrajectoryPoint], None until "Load && Send" succeeds.
        # Completely separate from any LIVE movement (manual moves,
        # initial/return trajectories), which lives entirely in
        # ConnectionScreen/InitialPositionSession and is never written
        # here — this attribute is ONLY ever assigned in
        # _on_send_clicked, and only replaced by loading a different
        # CSV, never mutated in place by any other flow.
        self._ensayo_trajectory = None
        # The trajectory_combo id `_ensayo_trajectory` was loaded from —
        # None until a load succeeds, set alongside it in
        # _on_send_clicked. Used only to key tara_library records (the
        # basal/no-contact reference recorded per-CSV via the Tara
        # button, see _on_tara_clicked/_refresh_tara_label) to the
        # right ensayo.
        self._ensayo_trajectory_id = None
        # The initial position `_ensayo_trajectory` was last successfully
        # sent for (see _send_and_check) — None until a send succeeds.
        # Run is only enabled when this matches the CURRENT
        # _position_session.position (see _refresh_controls): every time
        # a new initial position is set up (first GOTO after HOME, or
        # "Elegir Otro Ensayo" -> a new GOTO for a different trial), the
        # operator is expected to pick and load a CSV for THAT trial —
        # Run must stay blocked until they actually do, rather than
        # silently reusing whatever trajectory happened to be loaded
        # for a previous position. "Reiniciar Ensayo" is unaffected: it
        # reuses the SAME position (never calls position_session.set())
        # and re-sends _ensayo_trajectory itself via _send_and_check().
        self._ensayo_sent_for_position = None

        # Repeat-sequence state for "Reiniciar Ensayo" (added 2026-09-08):
        # _repeats_remaining counts chained restarts still owed AFTER the
        # one currently in flight; _repeats_total is the count entered in
        # the dialog, kept only to render "(current/total)" progress and
        # to know whether a "sequence complete" message is warranted (>1)
        # vs. plain single-restart behavior (1, or 0 when no sequence is
        # active at all — e.g. a normal Run). Both reset to 0 by
        # _cancel_repeat_sequence() on anything that should stop the
        # chain: a validation failure, a failed reposition/send/run, an
        # error, a disconnect, or "Elegir Otro Ensayo". See
        # _on_trajectory_finished for where the chaining actually happens
        # (only a REAL FINISHED continues it — an abort never fires that
        # signal, so pausing already halts the chain with no extra code).
        self._repeats_remaining = 0
        self._repeats_total = 0

        # Live-plot data buffers, filled incrementally by
        # trajectory_progress signals — but ONLY while _plot_active is
        # True (see _on_trajectory_progress), i.e. only between a
        # _begin_plot_session() and the matching _end_plot_session()
        # (see those methods). TRAJ_PROGRESS also fires for unrelated
        # live movement (manual moves, the initial/return synchronized
        # trajectories — see
        # SystemStateMachine.move_relative/safe_return_to_position),
        # since the wire protocol has no per-trajectory "source" tag;
        # _plot_active is what keeps those out of this ensayo's plot.
        # Never set this flag directly outside those two methods — see
        # their docstrings for why every exit path matters (a PAUSED
        # run aborted via "Reiniciar Ensayo"/"Elegir Otro Ensayo" ends
        # without ever firing trajectory_finished).
        self._plot_t = []
        self._plot_x = []
        self._plot_y = []
        self._plot_angle = []
        self._plot_active = False

        self._build_ui()
        # Wired here rather than passed at construction (see
        # _build_platform_panel): self.info_label doesn't exist yet at
        # the point the joystick itself is built.
        self._joystick.set_status_callback(self.info_label.setText)
        self._theme_manager.theme_changed.connect(self._apply_theme)
        self._connect_signals()
        self._refresh_trajectory_list()
        self._refresh_controls()
        # Reflect an already-completed calibration immediately if HOME
        # already succeeded before this screen was ever shown — same
        # parity check the old Monitor3DWindow.__init__ did.
        self._platform_view.set_calibration_space(
            self._bridge.state_machine.last_calibration_space
        )

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        # Outer column: the main content row (platform view | sidebar),
        # then a footer row for the status message — 2026-07-25 layout
        # pass moved that message out of the sidebar (it was crowding
        # EXECUTION) down to its own last row, bottom-left of the WHOLE
        # window, not just the sidebar column.
        outer = QVBoxLayout(self)
        outer.setSpacing(LAYOUT_SPACING)
        outer.setContentsMargins(
            LAYOUT_MARGIN, LAYOUT_MARGIN, LAYOUT_MARGIN, LAYOUT_MARGIN
        )

        content_row = QHBoxLayout()
        content_row.setSpacing(LAYOUT_SPACING)
        outer.addLayout(content_row, stretch=1)

        # Dominant element: the position/calibration monitor, formerly
        # its own "Monitor Posición" window — see _build_platform_panel.
        # Stretch 3:1 against the sidebar (was 2:1) — freed up by
        # shrinking every button below, per Luis's explicit request that
        # this view be the clear visual protagonist of the screen.
        self._build_platform_panel(content_row)

        # Secondary sidebar: trajectory selection/execution controls up
        # top, the (now small, reference-only) live plot pinned to the
        # bottom — two visually distinct "corners" without competing
        # with the platform view for attention. Narrower and lighter
        # than before this pass: every button here dropped from the
        # full touch size (70px) to the compact one (48px) or smaller,
        # both freeing width for the platform view and giving the plot
        # box the vertical room it was missing.
        sidebar_spacing = LAYOUT_SPACING // 2
        sidebar = QVBoxLayout()
        sidebar.setSpacing(sidebar_spacing)
        content_row.addLayout(sidebar, stretch=1)

        # A shorter combo than the app-wide INPUT_STYLE (48px) — this
        # sidebar needs the height back more than the combo needs the
        # usual generous touch target.
        slim_combo_style = f"QComboBox {{ min-height: 32px; font-size: {FONT_SIZE_NORMAL}px; }}"

        # --- Selection ---
        select_box = QGroupBox("TRAJECTORY SELECTION")
        select_layout = QVBoxLayout(select_box)

        self.trajectory_combo = QComboBox()
        self.trajectory_combo.setStyleSheet(slim_combo_style)
        select_layout.addWidget(self.trajectory_combo)

        # Time-scaling factor applied to the CSV's own recorded `t`
        # values on load (see _on_send_clicked) — the simulator cannot
        # reproduce real gait timing in real time yet, so every ensayo
        # (TIMED TRAJ_POINT, 4-field form) needs its whole timeline
        # stretched by this factor before being sent. Only affects the
        # CSV/gait ensayo — trajectories sent WITHOUT time (joystick,
        # posición inicial, retorno seguro) never go through this.
        scale_row = QHBoxLayout()
        scale_label = QLabel("Escala de tiempo (x):")
        scale_label.setStyleSheet(f"font-size: {FONT_SIZE_LARGE}px;")
        self.time_scale_spinbox = QDoubleSpinBox()
        # NOT slim_combo_style: that rule's selector is "QComboBox", so it
        # never actually matched this QDoubleSpinBox — this control was
        # rendering at Qt's tiny default size the whole time. Own style,
        # deliberately bigger than the rest of this slim sidebar (a wrong
        # value here silently mis-scales the whole ensayo — see the
        # MAX_SPEED_* discussion above — so it earns the extra touch size).
        self.time_scale_spinbox.setStyleSheet(f"""
            QDoubleSpinBox {{
                min-height: 48px;
                font-size: {FONT_SIZE_LARGE}px;
                padding: 2px 4px;
            }}
            QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
                width: 32px;
            }}
        """)
        # No fixed upper cap (per Luis's explicit choice) — only a
        # positive-value floor above 0, since a zero/negative scale
        # would collapse or invert the trajectory's timeline.
        self.time_scale_spinbox.setRange(0.01, 1_000_000.0)
        self.time_scale_spinbox.setDecimals(2)
        self.time_scale_spinbox.setSingleStep(1.0)
        self.time_scale_spinbox.setValue(30.0)
        scale_row.addWidget(scale_label)
        scale_row.addWidget(self.time_scale_spinbox, stretch=1)
        select_layout.addLayout(scale_row)

        row = QHBoxLayout()
        self.refresh_button = QPushButton("Refresh List")
        self.refresh_button.setStyleSheet(BUTTON_STYLE_SLIM)
        self.send_button = QPushButton("Load && Send")
        self.send_button.setStyleSheet(BUTTON_STYLE_SLIM)
        row.addWidget(self.refresh_button)
        row.addWidget(self.send_button)
        select_layout.addLayout(row)

        # Purely visual read-out (2026-09-18, Luis's explicit request:
        # "para confirmar, nada más") of the loaded ensayo's own
        # recorded first-row angle — the value the angle-alignment leg
        # (see _send_and_check) actually rotates the platform to on
        # Load && Send. No interaction, just confirmation — updated in
        # _on_send_clicked, reset to "—" whenever nothing valid is
        # loaded.
        self.csv_angle_label = QLabel("Ángulo inicial CSV: —")
        self.csv_angle_label.setStyleSheet(
            f"font-size: {FONT_SIZE_NORMAL}px; color: {COLOR_TEXT_MUTED()};"
        )
        select_layout.addWidget(self.csv_angle_label)

        # Max per-axis speed the just-sent (or just-rejected — see
        # TrajectorySpeedExceededError) trajectory actually implies —
        # added 2026-09-01 alongside the speed safety gate in
        # SystemStateMachine, so the operator can see the number instead
        # of only finding out from a rejection message or, worse, from
        # the motor itself. Refreshed after every action this screen
        # runs through _run_action (see _update_speed_label).
        self.speed_label = QLabel("Vel. máx: —")
        self.speed_label.setStyleSheet(
            f"font-size: {FONT_SIZE_NORMAL}px; color: {COLOR_TEXT_MUTED()};"
        )
        select_layout.addWidget(self.speed_label)

        # stretch=0 normally (plot box below claims the leftover space);
        # while the live plot is hidden (_LIVE_PLOT_VISIBLE, see
        # _build_plot_box) there's nothing left to claim it, so this box
        # gets a share instead — per Luis's request, that freed vertical
        # space should go to the sidebar's buttons, not sit blank.
        sidebar.addWidget(select_box, stretch=0 if self._LIVE_PLOT_VISIBLE else 1)

        # --- Tara (referencia basal, pruebas de contacto) ---
        # See _on_tara_clicked/_send_and_check's detach_tara param:
        # records the current (y, angle) as the "no contact" reference
        # for the loaded ensayo, so Run can automatically fuse a
        # detach-and-reapproach hop onto the ensayo before executing it
        # whenever the operator has since lowered Y to a contact-test
        # height. One tara per CSV (tara_library.py), updatable (the
        # button re-prompts before overwriting).
        tara_box = QGroupBox("TARA (REFERENCIA BASAL)")
        tara_layout = QVBoxLayout(tara_box)
        self.tara_label = QLabel("Carga un ensayo para ver/registrar su tara.")
        self.tara_label.setWordWrap(True)
        self.tara_label.setStyleSheet(f"font-size: {FONT_SIZE_NORMAL}px;")
        tara_layout.addWidget(self.tara_label)
        self.tara_button = QPushButton("Tara (guardar posición actual)")
        self.tara_button.setStyleSheet(BUTTON_STYLE_SLIM)
        tara_layout.addWidget(self.tara_button)
        sidebar.addWidget(tara_box, stretch=0)

        # --- Execution --- (no title text — see _NO_TITLE_GROUPBOX_STYLE)
        exec_box = QGroupBox("")
        exec_box.setStyleSheet(self._NO_TITLE_GROUPBOX_STYLE)
        exec_box_layout = QVBoxLayout(exec_box)
        exec_box_layout.setSpacing(sidebar_spacing)

        exec_layout = QHBoxLayout()
        self.run_button = QPushButton("Run")
        self.run_button.setStyleSheet(BUTTON_STYLE_PRIMARY_SLIM())
        self.pause_button = QPushButton("Pause")
        self.pause_button.setStyleSheet(BUTTON_STYLE_SLIM)
        self.resume_button = QPushButton("Resume")
        self.resume_button.setStyleSheet(BUTTON_STYLE_SLIM)

        exec_layout.addWidget(self.run_button)
        exec_layout.addWidget(self.pause_button)
        exec_layout.addWidget(self.resume_button)
        exec_box_layout.addLayout(exec_layout)

        # Only enabled while PAUSED — abandon the paused trial (see
        # SystemStateMachine.abort()) instead of resuming it, because
        # the operator observed a mechanical fault. Separate from
        # Run/Pause/Resume so it reads as a distinct action set.
        # Stacked vertically (one per row), not side by side — their
        # Spanish labels are long enough that splitting this narrower
        # sidebar's width in half clipped the text (a real bug caught in
        # this pass's own visual check); a full-width row each is
        # legible instead.
        retry_layout = QVBoxLayout()
        retry_layout.setSpacing(sidebar_spacing // 2)
        self.restart_trial_button = QPushButton("Reiniciar Ensayo")
        self.restart_trial_button.setStyleSheet(BUTTON_STYLE_SLIM)
        self.choose_other_button = QPushButton("Elegir Otro Ensayo")
        self.choose_other_button.setStyleSheet(BUTTON_STYLE_SLIM)
        retry_layout.addWidget(self.restart_trial_button)
        retry_layout.addWidget(self.choose_other_button)
        exec_box_layout.addLayout(retry_layout)

        sidebar.addWidget(exec_box, stretch=0 if self._LIVE_PLOT_VISIBLE else 1)

        # Plot box takes whatever's left in the sidebar column (stretch=1,
        # see _build_plot_box) — select_box/exec_box above are now
        # compact enough that this is real, usable space instead of a
        # sliver the "Limpiar Gráfica" button used to eat into.
        self._build_plot_box(sidebar)

        # --- Status message: last row of the WHOLE window, bottom-left ---
        footer = QHBoxLayout()
        self.info_label = QLabel("No trajectory loaded.")
        self.info_label.setStyleSheet(f"font-size: {FONT_SIZE_NORMAL}px;")
        self.info_label.setWordWrap(True)
        footer.addWidget(self.info_label, stretch=1)
        outer.addLayout(footer)

    def _build_platform_panel(self, content_row):
        """
        The position/calibration monitor (PlatformView + PositionPoller,
        see src/ui/platform_view.py) — this screen's dominant central
        element. Was its own top-level window ("Monitor Posición",
        monitor_3d_window.py/Monitor3DWindow) until 2026-07-25's
        consolidation; the widgets and their wiring are unchanged, only
        embedded here instead of in a separate QWidget subclass. The
        poller's lifecycle (start on show, stop on hide) is now driven
        by THIS screen's own showEvent/hideEvent (see below) rather than
        a dedicated window's.
        """
        panel = QVBoxLayout()
        panel.setSpacing(0)

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
            f"font-size: {FONT_SIZE_NORMAL}px; color: {COLOR_TEXT_MUTED()};"
        )
        info_layout.addWidget(self._calibration_label)
        panel.addWidget(info_bar)

        self._platform_view = PlatformView()
        panel.addWidget(self._platform_view, stretch=1)

        # Manual movement joystick (2026-07-25+ redesign, moved from
        # ConnectionScreen's old "Movimiento Manual" box) — floats in
        # the top-right corner of the visualization itself (parent=
        # self._platform_view, not the outer panel), see
        # src/ui/manual_joystick.py for why it's a plain floating child
        # rather than embedded in this layout.
        self._joystick = ManualJoystickControl(
            self._bridge, self._position_session, self._platform_view,
            self._theme_manager,
        )

        content_row.addLayout(panel, stretch=3)

    @staticmethod
    def _format_position_text(x: float, y: float, angle: float) -> str:
        # Colored to match the drawing's X/Y/angle encoding (and the
        # live trajectory plot's own palette below) — same axis, same
        # color, everywhere in the app. Called fresh on every position
        # update (~5x/s while this screen is visible), so it always
        # reflects whichever theme is currently active with no extra
        # re-application needed.
        return (
            f'<span style="color:{COLOR_AXIS_X()}">X:</span> {x:.1f} cm &nbsp;&nbsp; '
            f'<span style="color:{COLOR_AXIS_Y()}">Y:</span> {y:.1f} cm &nbsp;&nbsp; '
            f'<span style="color:{COLOR_AXIS_ANGLE()}">Á:</span> {angle:.1f}°'
        )

    def _on_position_received(self, position):
        self._platform_view.set_position(position.x, position.y, position.angle)
        self._position_label.setText(
            self._format_position_text(position.x, position.y, position.angle)
        )

    def showEvent(self, event):
        super().showEvent(event)
        if self._poller is None:
            self._poller = PositionPoller(self._bridge.state_machine, self)
            self._poller.position_received.connect(self._on_position_received)
            self._poller.start()
        # position_session is plain data (no Qt signal) — a move made on
        # ConnectionScreen (GOTO, safe return) while this screen was
        # hidden wouldn't otherwise be reflected in the joystick's
        # enabled state until the next unrelated state_changed.
        self._joystick.refresh_controls()

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

    # Title text for the 3 stacked plot rows — kept as one place so
    # _build_plot_box() and _apply_theme() (which re-sets each title's
    # color on a theme change) share the exact same strings.
    _PLOT_TITLES = (
        "Posición X vs Tiempo", "Posición Y vs Tiempo", "Ángulo vs Tiempo",
    )
    _PLOT_TITLE_SIZE = "9pt"

    # Hidden per Luis's request (2026-09-08): pos_x/pos_y live feedback
    # isn't implemented yet on the real firmware, so the plot has nothing
    # real to show. All the plotting machinery below is left fully built
    # (still fed by _on_trajectory_progress) so this is a one-line
    # re-enable once that's ready — just flip this back to True. The
    # freed sidebar space goes to select_box/exec_box instead (see their
    # stretch factors in _build_ui) rather than sitting blank.
    _LIVE_PLOT_VISIBLE = False

    # Overrides ONLY the title-reserved space (margin-top/padding-top)
    # that the app-wide QGroupBox rule (style.py's APP_STYLESHEET)
    # always reserves for a title, whether one is set or not — used on
    # the two boxes Luis asked to drop their title text from entirely
    # (EXECUTION, LIVE TRAJECTORY PLOT) to give that space back to the
    # live plot. Every other QGroupBox property (background/border/
    # font) still cascades from the global rule; Qt merges per-widget
    # stylesheets property-by-property, it doesn't replace the whole rule.
    _NO_TITLE_GROUPBOX_STYLE = "QGroupBox { margin-top: 4px; padding-top: 6px; }"

    def _build_plot_box(self, root):
        # Aligning to the app's actual COLOR_BG()/COLOR_TEXT() so the
        # plot's real rendered background matches the surface the axis
        # pen colors (COLOR_AXIS_X/Y/ANGLE) were validated against
        # (dataviz skill, dark theme only — see style.py's module
        # docstring on the light palette's contrast being a first pass).
        # setConfigOptions() only affects plots created AFTER this call;
        # _apply_theme() below re-colors this SAME plot_widget/curves in
        # place for a live theme change.
        pg.setConfigOptions(background=COLOR_BG(), foreground=COLOR_TEXT())

        # No fixed setMaximumHeight cap (2026-07-25 layout pass removed
        # it — it was capping this box BELOW what it needed, which is
        # exactly why "Limpiar Gráfica" ended up overlapping the plot).
        # Instead this box gets stretch=1 in the sidebar (see below) and
        # claims whatever's left after the now-compact select_box/exec_box
        # above it.
        # No title text (see _NO_TITLE_GROUPBOX_STYLE) — every bit of
        # height in this box goes to the graph now.
        plot_box = QGroupBox("")
        plot_box.setStyleSheet(self._NO_TITLE_GROUPBOX_STYLE)
        plot_box.setVisible(self._LIVE_PLOT_VISIBLE)
        plot_layout = QVBoxLayout(plot_box)

        self._plot_widget = pg.GraphicsLayoutWidget()
        # Tighter row spacing than pyqtgraph's default — needed now that
        # this widget is a small sidebar reference panel rather than a
        # half-window plot (2026-07-25 consolidation), so 3 stacked rows
        # still fit without the bottom one clipping off.
        self._plot_widget.ci.layout.setSpacing(2)

        # No "left" axis label text on any row (each title already says
        # what it plots, e.g. "Posición X vs Tiempo") — pyqtgraph draws
        # that label rotated alongside the row, which ate a lot of
        # width/height for text that's redundant with the title once
        # rows are this short. The Y-axis itself (with its cm/deg tick
        # values) stays, only the extra text label is dropped.
        x_title, y_title, angle_title = self._PLOT_TITLES
        self._x_plot = self._plot_widget.addPlot(row=0, col=0)
        self._x_plot.setTitle(x_title, size=self._PLOT_TITLE_SIZE)

        self._y_plot = self._plot_widget.addPlot(row=1, col=0)
        self._y_plot.setTitle(y_title, size=self._PLOT_TITLE_SIZE)

        self._angle_plot = self._plot_widget.addPlot(row=2, col=0)
        self._angle_plot.setTitle(angle_title, size=self._PLOT_TITLE_SIZE)

        self._y_plot.setXLink(self._x_plot)
        self._angle_plot.setXLink(self._x_plot)

        # X and Y are already X-linked to Angle (same shared timeline —
        # see setXLink above), so their own bottom-axis tick VALUES are
        # redundant now that all 3 rows have to fit in a much smaller
        # panel: only the bottom-most plot (Angle) needs to show them.
        # Standard shared-x-axis compaction — no data or interaction is
        # lost, panning/zooming any one row still moves all three.
        self._x_plot.getAxis("bottom").setStyle(showValues=False)
        self._y_plot.getAxis("bottom").setStyle(showValues=False)

        self._x_curve = self._x_plot.plot(pen=pg.mkPen(color=COLOR_AXIS_X(), width=2))
        self._y_curve = self._y_plot.plot(pen=pg.mkPen(color=COLOR_AXIS_Y(), width=2))
        self._angle_curve = self._angle_plot.plot(pen=pg.mkPen(color=COLOR_AXIS_ANGLE(), width=2))

        # The graph itself gets all the stretch — the button below it
        # (stretch=0, its own natural/minimal size) must never be able
        # to squeeze the plot area smaller than it needs, which is
        # exactly how it ended up overlapping/covering the graph before
        # this pass.
        plot_layout.addWidget(self._plot_widget, stretch=1)

        # BUTTON_STYLE_SLIM (same as every other button in this
        # sidebar) — this one specifically must never claim more height
        # than it needs, since it shares this panel with the graph
        # itself (see the graph's own stretch=1 above).
        self.clear_plot_button = QPushButton("Limpiar Gráfica")
        self.clear_plot_button.setStyleSheet(BUTTON_STYLE_SLIM)
        self.clear_plot_button.clicked.connect(self._clear_plot)
        plot_layout.addWidget(self.clear_plot_button, stretch=0)

        root.addWidget(plot_box, stretch=1)

    def _apply_theme(self, _name: str) -> None:
        """
        Re-applies every color this screen bakes into a widget's own
        stylesheet/pen at construction time (see style.py's module
        docstring for why that's necessary at all). PlatformView isn't
        touched here — it reads COLOR_*() fresh inside its own
        paintEvent already, so it re-colors itself on its own next
        repaint; calling update() just makes that happen immediately
        instead of waiting for the position poller's next ~200ms tick.
        """
        self.run_button.setStyleSheet(BUTTON_STYLE_PRIMARY_SLIM())
        self._calibration_label.setStyleSheet(
            f"font-size: {FONT_SIZE_NORMAL}px; color: {COLOR_TEXT_MUTED()};"
        )

        bg, fg = COLOR_BG(), COLOR_TEXT()
        pg.setConfigOptions(background=bg, foreground=fg)
        self._plot_widget.setBackground(bg)
        for plot_item, title in zip(
            (self._x_plot, self._y_plot, self._angle_plot), self._PLOT_TITLES
        ):
            plot_item.setTitle(title, color=fg, size=self._PLOT_TITLE_SIZE)
            for axis_name in ("left", "bottom"):
                axis = plot_item.getAxis(axis_name)
                axis.setPen(fg)
                axis.setTextPen(fg)
        self._x_curve.setPen(pg.mkPen(color=COLOR_AXIS_X(), width=2))
        self._y_curve.setPen(pg.mkPen(color=COLOR_AXIS_Y(), width=2))
        self._angle_curve.setPen(pg.mkPen(color=COLOR_AXIS_ANGLE(), width=2))

        self._platform_view.update()

    def _connect_signals(self):
        self.refresh_button.clicked.connect(self._refresh_trajectory_list)
        self.send_button.clicked.connect(self._on_send_clicked)
        self.run_button.clicked.connect(self._on_run_clicked)
        self.pause_button.clicked.connect(self._on_pause_clicked)
        self.resume_button.clicked.connect(self._on_resume_clicked)
        self.restart_trial_button.clicked.connect(self._on_restart_trial_clicked)
        self.choose_other_button.clicked.connect(self._on_choose_other_clicked)
        self.tara_button.clicked.connect(self._on_tara_clicked)

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
            self.csv_angle_label.setText("Ángulo inicial CSV: —")
            return

        try:
            loaded = load_trajectory_by_id(trajectory_id)
        except Exception as exc:
            self.info_label.setText(f"Failed to load '{trajectory_id}': {exc}")
            self._ensayo_trajectory = None
            self._ensayo_trajectory_id = None
            self.csv_angle_label.setText("Ángulo inicial CSV: —")
            return

        # Stretch the whole recorded timeline by the chosen factor
        # (default 30x, see time_scale_spinbox above) — the simulator
        # can't reproduce real gait speed yet, so every `t` value is
        # scaled up before this trajectory is ever offset/validated/
        # sent. x/y/angle are untouched: only how long the ESP32 takes
        # to traverse the same points changes. Applied once here, at
        # load time, so "Reiniciar Ensayo" (which resends this same
        # `_ensayo_trajectory`) automatically reuses the already-scaled
        # timeline without reapplying the factor.
        time_scale = self.time_scale_spinbox.value()
        self._ensayo_trajectory = [
            TrajectoryPoint(t=p.t * time_scale, x=p.x, y=p.y, angle=p.angle)
            for p in loaded
        ]
        self._ensayo_trajectory_id = trajectory_id
        self.csv_angle_label.setText(
            f"Ángulo inicial CSV: {self._ensayo_trajectory[0].angle:.1f}°"
        )
        self._refresh_tara_label()
        # Whatever position the PREVIOUS trajectory (if any) was sent
        # for is no longer relevant — Run stays blocked until THIS one
        # is actually sent (see _send_and_check / _refresh_controls).
        self._ensayo_sent_for_position = None

        if not self._check_ensayo_within_range("Ensayo rechazado"):
            return

        self.info_label.setText(
            f"Loaded '{trajectory_id}' ({len(self._ensayo_trajectory)} points). Sending..."
        )
        self._run_action(
            lambda: self._send_and_check(),
            success_message=f"'{trajectory_id}' sent and stored. Ready to run."
        )

    def _check_ensayo_within_range(self, context_message: str) -> bool:
        """
        UI-thread pre-check so an out-of-range ensayo shows the visual
        LimitViolationDialog (map + highlighted offending point) instead
        of only the generic worker-failure text. _send_and_check() still
        re-validates for real inside the background worker (the actual
        safety gate, shared by "Load && Send" and "Reiniciar Ensayo") —
        this is purely a presentation-layer preview of that same check.

        Returns False (caller must not proceed) only for a real
        out-of-range violation. If there's no calibration data yet,
        returns True and lets the normal flow surface that different
        problem instead (not a map-limits issue).
        """
        space = self._bridge.state_machine.last_calibration_space
        if space is None:
            return True
        points = self._offset_points(self._ensayo_trajectory)
        try:
            validate_trajectory(points, space)
        except TrajectoryOutOfRangeError as exc:
            LimitViolationDialog.from_trajectory_error(
                space, context_message, points, exc, parent=self,
            ).exec()
            return False
        return True

    def _send_and_check(self, detach_tara: Position = None):
        """
        Runs on the background thread. Validates every point of the
        fully-offset trajectory against the calibrated movement space
        BEFORE anything is sent to the ESP32 — rejects the whole ensayo
        if any point is out of range (see
        src/utils/trajectory_validator.py). Unlike the generated
        initial/return trajectories, a CSV-loaded ensayo is arbitrary
        data with no guarantee of staying within a straight line
        between two already-valid endpoints, so every point (not just
        the ends) must be checked individually. This is the single
        choke point "Load && Send", "Reiniciar Ensayo", AND Run-with-
        detach (see below) all go through, so none can bypass the check.

        detach_tara: when given (Run/Reiniciar Ensayo with a tara on
        record for this ensayo), FUSES a platform-detach hop directly
        onto the ensayo's points into ONE trajectory/ONE transfer (see
        trajectory_generator.generate_detach_and_ensayo_trajectory()'s
        docstring for the full reasoning) instead of the old two-
        trajectory sequence (SystemStateMachine.perform_pre_run_detach(),
        removed 2026-09-18) — that version sent/ran the hop, blocked
        until it physically finished, and only THEN transferred the
        ensayo as a SEPARATE trajectory, leaving a real dead pause on
        the rig for however long that second transfer took. The hop's
        own duration is now computed from MAX_SPEED_X_CM_S/_Y_CM_S
        directly (fastest safe pace) rather than the ensayo's own
        time_scale-stretched one (revised 2026-09-18, same day: sharing
        time_scale made a 1cm hop feel "bastante lento" at the default
        30x) — since the combined trajectory is still sent TIMED, it's
        also still subject to that same ceiling as a backstop.

        ALSO fuses an angle-alignment leg ahead of everything else
        (2026-09-18, unconditional — not gated by `detach_tara`) so the
        platform physically rotates to `self._ensayo_trajectory`'s own
        recorded first-row angle before the ensayo (or the detach hop,
        if present) begins — see
        trajectory_generator.generate_angle_alignment_trajectory()'s
        docstring for why. A no-op (no leg generated at all) if the
        current angle already matches closely enough. When it DOES
        rotate, `_offset_points()` is called with an `effective_position`
        override (same x/y, angle = the ensayo's own target) instead of
        `self._position_session.position` directly, and the detach
        hop's own `current` (when both are present) is taken from
        where the alignment leg ends, NOT the platform's real position
        right now — otherwise the hop would detach/reapproach at the
        OLD angle instead of the ensayo's.

        send_trajectory() itself does not raise on a failed transfer
        (it returns a result object) — we raise here so the worker's
        failed/succeeded signals reflect the real outcome.

        send_trajectory() ALSO rejects (TrajectorySpeedExceededError,
        never transmitted) any point implying an unsafe per-axis speed
        — see SystemStateMachine.MAX_SPEED_X_CM_S/_Y_CM_S/_ANGLE_DEG_S.
        Added 2026-09-01 after a real Y-axis overspeed caused by a stale
        `_position_session` offset landing in the ensayo's timed first
        point (see that exception's docstring); this is a second,
        independent gate from the position-bounds check above — a point
        can be well within the calibrated space and still be unsafely
        FAST to get to from wherever the platform actually is.
        """
        sm = self._bridge.state_machine
        space = sm.last_calibration_space
        if space is None:
            raise RuntimeError(
                "No hay datos de calibración disponibles; no se puede "
                "validar el ensayo."
            )

        align_leg = []
        effective_position = self._position_session.position
        if self._ensayo_trajectory and effective_position is not None:
            target_angle = self._ensayo_trajectory[0].angle
            align_leg = trajectory_generator.generate_angle_alignment_trajectory(
                sm.get_position(), target_angle,
                self.time_scale_spinbox.value(), space,
            )
            if align_leg:
                effective_position = Position(
                    effective_position.x, effective_position.y, target_angle,
                )

        points = self._offset_points(self._ensayo_trajectory, effective_position)

        if detach_tara is not None:
            detach_current = (
                Position(align_leg[-1].x, align_leg[-1].y, align_leg[-1].angle)
                if align_leg else sm.get_position()
            )
            points = trajectory_generator.generate_detach_and_ensayo_trajectory(
                detach_current, detach_tara.y,
                sm.DETACH_LIFT_ABOVE_TARA_CM, sm.DETACH_X_SHIFT_CM,
                points, sm.MAX_SPEED_X_CM_S, sm.MAX_SPEED_Y_CM_S, space,
            )

        # Final 5cm lift-off (2026-09-21), fused onto the END of the same
        # trajectory so the platform rises the instant the ensayo's last
        # point is reached, with no second transfer/gap — see
        # trajectory_generator.append_end_lift. Unconditional (not gated
        # by a tara), since this is the single choke point Load && Send,
        # a plain Run, Run-with-detach and Reiniciar Ensayo all share.
        points = trajectory_generator.append_end_lift(
            points, sm.END_LIFT_CM,
            sm.MAX_SPEED_X_CM_S, sm.MAX_SPEED_Y_CM_S, space,
        )

        if align_leg:
            points = trajectory_generator.stitch_trajectories([align_leg, points])

        try:
            validate_trajectory(points, space)
        except TrajectoryOutOfRangeError as exc:
            raise RuntimeError(f"Ensayo rechazado: {exc}") from exc

        # The CSV/gait ensayo IS real recorded gait timing — the one
        # case that needs the TIMED TRAJ_POINT form (see docs/protocol.md,
        # "Cambio 2026-08-31 (TRAJ_POINT sin tiempo)"). The fused detach
        # hop (when present) rides along on the SAME timed send — see
        # detach_tara's docstring above for why that's now correct.
        try:
            result = sm.send_trajectory(points, timed=True)
        except TrajectorySpeedExceededError as exc:
            raise RuntimeError(f"Ensayo rechazado: {exc}") from exc
        if not result.success:
            raise RuntimeError(
                f"Transfer failed after {result.points_acknowledged} "
                f"points: {result.error}"
            )
        # Records exactly which position this send was for — Run is
        # only enabled while this still matches _position_session.position
        # (see _refresh_controls). Any later change to the initial
        # position (a new GOTO, e.g. via "Elegir Otro Ensayo") makes this
        # stale again, requiring a fresh Load && Send before Run unlocks.
        self._ensayo_sent_for_position = self._position_session.position

    def _offset_points(self, points, position=None):
        """
        Shift the WHOLE loaded trajectory so its own FIRST point lands
        exactly on `position` (defaults to
        `self._position_session.position`, the initial position set up
        in ConnectionScreen — GOTO already moved the rig there) — the
        offset on every axis (X, Y, AND angle) is computed relative to
        the trajectory's own first point, not just added on top of the
        initial position.

        `position` accepts an override (see _send_and_check's
        angle-alignment leg, 2026-09-18): when the ensayo's own
        starting angle is about to be reached for real via a fused
        alignment leg, the OFFSET math needs to already assume that
        angle rather than whatever stale value
        `self._position_session.position.angle` still holds at the
        moment this runs (the alignment hasn't physically happened
        yet — it's part of the SAME trajectory being built). X/Y are
        unaffected either way.

        Bug fixed here (2026-07-25): the old formula (`p.axis +
        position.axis`, no subtraction) implicitly assumed each CSV's
        first row was already (x=0, y=0, angle=0). That happens to hold
        for X/Y in practice (these trials are recorded starting from
        that origin), so it looked like "X/Y already do this correctly"
        — but it does NOT hold for angle, whose CSV values are real
        recorded joint angles that rarely start at 0. A trajectory
        whose first angle was e.g. -20 would jump straight to
        `position.angle - 20` on Run instead of starting from wherever
        the rig actually was. Subtracting the trajectory's own first
        point on every axis fixes that for angle while being a no-op
        for X/Y (their first point is already ~0, so nothing changes
        there) — one shared formula, not a special angle-only case.

        If no initial position was set up, or the trajectory is empty,
        points are sent as-is (same as before — there's no "current
        position" to reference against, or nothing to reference).
        """
        if position is None:
            position = self._position_session.position
        if position is None or not points:
            return points
        first = points[0]
        return [
            TrajectoryPoint(
                t=p.t,
                x=p.x - first.x + position.x,
                y=p.y - first.y + position.y,
                angle=p.angle - first.angle + position.angle,
            )
            for p in points
        ]

    # ------------------------------------------------------------------
    # Tara (basal / no-contact reference)
    # ------------------------------------------------------------------

    def _on_tara_clicked(self):
        """
        Records the CURRENT tracked position (position_session.position
        — the same value the joystick's manual nudges keep in sync, see
        InitialPositionSession.apply_manual_delta) as the "no contact"
        basal reference for the loaded ensayo. Confirms before
        overwriting an existing tara (the "corrección futura" flow) —
        tara_library.save_tara() itself always overwrites
        unconditionally, so this confirmation is the only thing
        standing between a mis-tap and losing the previous reference.
        """
        if self._ensayo_trajectory_id is None:
            self.info_label.setText("Carga un ensayo antes de registrar su tara.")
            return
        position = self._position_session.position
        if position is None:
            self.info_label.setText("No hay una posición actual registrada.")
            return

        if tara_library.has_tara(self._ensayo_trajectory_id):
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Question)
            box.setWindowTitle("Actualizar Tara")
            box.setText(
                f"Ya existe una tara guardada para "
                f"'{self._ensayo_trajectory_id}'. ¿Deseas reemplazarla con "
                f"la posición actual (Y={position.y:.2f}cm, "
                f"Á={position.angle:.1f}°)? El historial de pruebas ya "
                f"registrado se conserva."
            )
            update_btn = box.addButton("Actualizar", QMessageBox.AcceptRole)
            box.addButton("Cancelar", QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() is not update_btn:
                return

        tara_library.save_tara(self._ensayo_trajectory_id, position)
        self.info_label.setText(
            f"Tara guardada para '{self._ensayo_trajectory_id}' "
            f"(Y={position.y:.2f}cm, Á={position.angle:.1f}°)."
        )
        self._refresh_tara_label()

    def _refresh_tara_label(self):
        sm = self._bridge.state_machine
        self.tara_button.setEnabled(
            sm.can_run()
            and self._ensayo_trajectory_id is not None
            and self._position_session.position is not None
        )
        if self._ensayo_trajectory_id is None:
            self.tara_label.setText("Carga un ensayo para ver/registrar su tara.")
            return
        record = self._load_tara_for_current_ensayo()
        if record is None:
            self.tara_label.setText(
                f"'{self._ensayo_trajectory_id}': sin tara registrada."
            )
            return
        self.tara_label.setText(
            f"Tara '{self._ensayo_trajectory_id}': "
            f"Y={record.tara.y:.2f}cm  Á={record.tara.angle:.1f}°  "
            f"({len(record.pruebas)} prueba(s) registrada(s))"
        )

    def _on_run_clicked(self):
        sm = self._bridge.state_machine
        tara_record = self._load_tara_for_current_ensayo()

        if tara_record is None:
            # No tara on record for this ensayo — unchanged behavior
            # (Luis's explicit choice: this flow is opt-in per CSV, via
            # the Tara button, not mandatory).
            self._clear_plot()
            self._begin_plot_session()
            self._run_action(sm.run, success_message="Running...")
            return

        def do_run_with_detach():
            # Detach hop + ensayo FUSED into one send/run (2026-09-18,
            # see _send_and_check's detach_tara param and
            # trajectory_generator.generate_detach_and_ensayo_trajectory) —
            # no separate blocking detach move before this anymore, so
            # there's no gap between "the hop finishes" and "the ensayo
            # starts" for a second transfer to sit in. The tara's own Y
            # (where the whole combined run starts/detaches from) is the
            # contact-test value Luis wants on record.
            tara_library.add_prueba(self._ensayo_trajectory_id, tara_record.tara.y)
            self._send_and_check(detach_tara=tara_record.tara)
            self._begin_plot_session()
            sm.run()

        self._clear_plot()
        self.info_label.setText("Enviando despegue + ensayo...")
        self._run_action(do_run_with_detach, success_message="Running...")

    def _load_tara_for_current_ensayo(self):
        """None if no ensayo is loaded, or none has a tara on record."""
        if self._ensayo_trajectory_id is None:
            return None
        try:
            return tara_library.load_tara(self._ensayo_trajectory_id)
        except tara_library.TaraNotFoundError:
            return None

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
        Asks how many times to run the SAME trial (1 = today's plain
        single restart, unchanged; N>1 = auto-chain N restarts, one per
        FINISHED — see _on_trajectory_finished) before doing the first
        one. QInputDialog.getInt is the "floating window" Luis asked
        for — same pattern already used elsewhere in this screen (e.g.
        _offer_save_as_new_position) rather than a bespoke QDialog.
        """
        count, ok = QInputDialog.getInt(
            self, "Repetir Ensayo",
            "¿Cuántas veces ejecutar este ensayo? (1 = una sola vez)",
            1, 1, 999,
        )
        if not ok:
            return
        self._repeats_total = count
        self._repeats_remaining = count - 1
        self._restart_trial()

    def _restart_trial(self):
        """
        Restarts the SAME trial: reposition to the SAME initial point
        used last, then resend the SAME loaded trajectory and run it —
        no manual "Load && Send"/"Run" presses needed. Resending (rather
        than relying on the ESP32 still having it stored from before)
        keeps this independent of how long TRAJ_STORED persists across
        an ABORT on whatever firmware is running.

        Called both directly (single restart / first leg of a repeat
        sequence, from _on_restart_trial_clicked) and automatically by
        _on_trajectory_finished to run the next leg of a repeat
        sequence — _repeats_total/_repeats_remaining (set by the
        caller before this runs) only drive the progress text here;
        this method itself just performs one leg.
        """
        sm = self._bridge.state_machine
        was_paused = sm.can_abort()

        target = self._position_session.position
        if target is None:
            self.info_label.setText("No hay una posición inicial previa registrada.")
            self._cancel_repeat_sequence()
            return
        if self._ensayo_trajectory is None:
            self.info_label.setText("No hay una trayectoria cargada para reiniciar.")
            self._cancel_repeat_sequence()
            return
        if not self._check_ensayo_within_range("Ensayo rechazado"):
            self._cancel_repeat_sequence()
            return
        floor_y = target.y

        def do_restart():
            # Unconditional, even if was_paused is False: a PAUSED run
            # leaves _plot_active True (see _begin_plot_session) since
            # abort() ends it without ever firing trajectory_finished —
            # this must not depend on remembering that elsewhere, so
            # the reposition phase below always starts from a clean,
            # explicitly-deactivated state regardless of what happened
            # before this call.
            self._end_plot_session()
            if was_paused:
                sm.abort()
            sm.safe_return_to_position(target, floor_y)
            # Same fused detach-before-run rule as a plain Run (see
            # _on_run_clicked/_send_and_check's detach_tara param) —
            # applied on EVERY repetition, not just the first:
            # safe_return_to_position() above already brings the rig
            # back down to `target` (the same contact-test Y as
            # before), so skipping this here would reintroduce the
            # exact "Run starts already in contact" problem on every
            # leg of a repeat sequence.
            tara_record = self._load_tara_for_current_ensayo()
            if tara_record is not None:
                tara_library.add_prueba(self._ensayo_trajectory_id, tara_record.tara.y)
                self._send_and_check(detach_tara=tara_record.tara)
            else:
                self._send_and_check()
            self._begin_plot_session()
            sm.run()

        current_rep = self._repeats_total - self._repeats_remaining
        progress = (
            f" ({current_rep}/{self._repeats_total})"
            if self._repeats_total > 1 else ""
        )
        self._clear_plot()
        self.info_label.setText(f"Regresando a la posición inicial...{progress}")
        self._run_action(
            do_restart,
            success_message=f"Ensayo reiniciado. Running...{progress}",
            min_duration_ms=3000,
            on_failure=self._cancel_repeat_sequence,
        )

    def _cancel_repeat_sequence(self):
        """
        Stops a "Reiniciar Ensayo" repeat sequence from chaining any
        further — called on anything that means the operator (or a
        failure) is no longer expecting it to continue: a failed leg,
        a device error, a disconnect, or "Elegir Otro Ensayo". A plain
        single restart (count=1) is a no-op case of this same state
        (_repeats_total already 0 by the time FINISHED arrives), so
        nothing else needs to special-case "was this ever a sequence".
        """
        self._repeats_remaining = 0
        self._repeats_total = 0

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
        # Same reasoning as do_restart(): abort() (below) ends a PAUSED
        # run without ever firing trajectory_finished, so _plot_active
        # would otherwise stay True — and the operator is about to go
        # perform a temporal move (retorno/inicial) on ConnectionScreen
        # for the NEXT trial, which must never write into this
        # (now-abandoned) ensayo's plot.
        self._end_plot_session()
        # Also the one place a mid-sequence "Reiniciar Ensayo" repeat
        # count gets abandoned: choosing a different trial entirely
        # means whatever repeats were still pending no longer apply.
        self._cancel_repeat_sequence()
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
        min_duration_ms: int = 0, on_failure=None,
    ):
        """
        min_duration_ms: keeps whatever the info_label already shows
        (set by the caller right before this) visible for at least this
        long before switching to success_message — without this, a fast
        action (e.g. the test firmware's near-instantaneous simulated
        GOTO) can flip the label before the operator has time to read
        it. Only pads the display, never delays a slow real action
        beyond however long it actually takes.

        on_failure: extra cleanup beyond the generic failed-message
        display (_on_action_failed) — currently only used by
        "Reiniciar Ensayo" to cancel a pending repeat sequence when a
        leg fails partway through, since no FINISHED will ever arrive
        for that leg to chain the next one from.
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

        def handle_failure(message):
            self._on_action_failed(message)
            if on_failure is not None:
                on_failure()

        self._worker.succeeded.connect(handle_success)
        self._worker.failed.connect(handle_failure)
        self._worker.start()

    def _on_action_succeeded(self, message: str):
        self.info_label.setText(message)
        self._update_speed_label()
        self._refresh_controls()

    def _on_action_failed(self, message: str):
        self.info_label.setText(f"Failed: {message}")
        self._update_speed_label()
        self._refresh_controls()

    def _update_speed_label(self):
        """
        Reflects SystemStateMachine.last_trajectory_max_speeds — the max
        per-axis speed implied by whichever trajectory this screen's
        _run_action last attempted to send (Load && Send, Reiniciar
        Ensayo). Called on BOTH success and failure: a rejection by
        TrajectorySpeedExceededError still updates the underlying value
        (see that exception's docstring), so this is exactly how an
        operator sees WHY a send was rejected, in the same units as the
        MAX_SPEED_* limits themselves — not just from the text message.
        Harmless no-op-looking call after an unrelated action (e.g. Run,
        which doesn't recompute deltas) — it just redraws the same
        number already showing.
        """
        sm = self._bridge.state_machine
        x, y, angle = sm.last_trajectory_max_speeds
        exceeded = (
            x > sm.MAX_SPEED_X_CM_S
            or y > sm.MAX_SPEED_Y_CM_S
            or angle > sm.MAX_SPEED_ANGLE_DEG_S
        )
        self.speed_label.setText(
            f"Vel. máx: X={x:.1f} cm/s   Y={y:.1f} cm/s   Á={angle:.1f} °/s"
        )
        color = COLOR_DANGER() if exceeded else COLOR_TEXT_MUTED()
        self.speed_label.setStyleSheet(
            f"font-size: {FONT_SIZE_NORMAL}px; color: {color};"
        )

    # ------------------------------------------------------------------
    # Reacting to state changes
    # ------------------------------------------------------------------

    def _on_state_changed(self, state_name: str):
        self._refresh_controls()
        self._joystick.refresh_controls()
        # HOME's limit-mapping sweep ends with the HOMING -> IDLE
        # transition — same trigger CalibrationMapWindow uses to refresh
        # its own view (see calibration_map_window.py), so the platform
        # panel's calibrated-bounds overlay updates the moment a HOME
        # completes, same as it did as a standalone Monitor3DWindow.
        if state_name == "IDLE":
            self._platform_view.set_calibration_space(
                self._bridge.state_machine.last_calibration_space
            )

    def _on_trajectory_finished(self):
        # No more TRAJ_PROGRESS is expected once FINISHED arrives, but
        # end the session anyway so a later, unrelated movement (e.g. a
        # manual move back on ConnectionScreen) can never be mistaken
        # for this ensayo's own execution if this screen is revisited.
        self._end_plot_session()

        # "Reiniciar Ensayo" repeat sequence: a REAL FINISHED is the
        # only thing that chains to the next leg — an abort (Pause ->
        # Elegir Otro Ensayo) never fires this signal at all, so simply
        # pausing already halts the chain with no extra guard needed
        # (see _cancel_repeat_sequence, called there anyway for
        # explicitness/robustness). _repeats_remaining is 0 for both a
        # plain single restart and a normal Run, so this only ever
        # takes the branch below during an actual N>1 sequence.
        if self._repeats_remaining > 0:
            self._repeats_remaining -= 1
            self._refresh_controls()
            self._restart_trial()
            return

        if self._repeats_total > 1:
            self.info_label.setText(
                f"Secuencia completa ({self._repeats_total}/{self._repeats_total})."
            )
        else:
            self.info_label.setText("Trajectory FINISHED.")
        self._repeats_total = 0
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

    def _begin_plot_session(self) -> None:
        """
        Marks the ensayo's own execution as the thing _on_trajectory_progress
        should plot, from right now until _end_plot_session(). Call
        this ONLY immediately before the specific sm.run() that starts
        the ensayo itself (see _on_run_clicked / do_restart) — never
        earlier, so any preceding reposition/abort in the same flow is
        excluded.
        """
        self._plot_active = True

    def _end_plot_session(self) -> None:
        """
        The inverse of _begin_plot_session() — safe (and required) to
        call defensively even when a session might not be active,
        since a PAUSED run that gets abandoned (abort(), via "Reiniciar
        Ensayo" or "Elegir Otro Ensayo") ends WITHOUT ever firing
        trajectory_finished, so nothing else would otherwise clear this
        before the next temporal move (retorno/inicial) starts firing
        its own TRAJ_PROGRESS. Every place this screen's own execution
        can end — real FINISHED, a device error, a disconnect, or an
        abort initiated from here — must call this.
        """
        self._plot_active = False

    def _on_trajectory_progress(self, t: float, x: float, y: float, angle: float):
        # Fires for ANY trajectory currently executing on the shared
        # bridge — manual moves, the initial/return synchronized
        # trajectories, and this ensayo's own Run/Reiniciar Ensayo all
        # emit the same TRAJ_PROGRESS event. Only append between a
        # _begin_plot_session() and its matching _end_plot_session() —
        # otherwise an unrelated movement would mix into this plot.
        if not self._plot_active:
            return
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
        # An error during RUNNING falls back to IDLE without ever
        # reporting FINISHED (see SystemStateMachine._on_device_error) —
        # end the session so a later, unrelated movement isn't mistaken
        # for a continuation of this (now-aborted) ensayo. Also cancels
        # any pending "Reiniciar Ensayo" repeat count for the same
        # reason: no FINISHED will arrive to chain the next leg from.
        self._end_plot_session()
        self._cancel_repeat_sequence()
        self.info_label.setText(f"ERROR [{code}]: {message}")
        show_device_error(self, "_device_error_dialog", self, code, message)

    def _on_disconnected(self):
        self._end_plot_session()
        self._cancel_repeat_sequence()
        self.info_label.setText("Disconnected.")
        self._refresh_controls()

    def _refresh_controls(self):
        sm = self._bridge.state_machine

        self.send_button.setEnabled(sm.can_send_trajectory())

        # Run additionally requires a CSV to have been loaded AND sent
        # for the CURRENT initial position (see _ensayo_sent_for_position's
        # comment in __init__) — otherwise the operator could press Run
        # right after setting up a new trial's position (e.g. via
        # "Elegir Otro Ensayo") without ever picking a csv for it,
        # executing a stale or nonexistent trajectory instead.
        ensayo_ready_to_run = (
            self._ensayo_trajectory is not None
            and self._ensayo_sent_for_position is not None
            and self._ensayo_sent_for_position == self._position_session.position
        )
        self.run_button.setEnabled(sm.can_run() and ensayo_ready_to_run)
        self.run_button.setToolTip(
            "" if ensayo_ready_to_run
            else "Selecciona un ensayo (CSV) y presiona 'Load && Send' antes de iniciar."
        )

        self.pause_button.setEnabled(sm.can_pause())
        self.resume_button.setEnabled(sm.can_resume())

        # Available both while PAUSED (fault observed mid-run) and while
        # IDLE after a trial FINISHED — NOT the very first IDLE right
        # after HOME, before anything has run yet, hence the extra
        # _ensayo_trajectory/position checks. can_run() covers "IDLE" without
        # importing SystemState here (screens use the state machine's
        # query methods, not the enum, per the project's layering rule).
        can_retry = (
            (sm.can_abort() or sm.can_run())
            and self._ensayo_trajectory is not None
            and self._position_session.position is not None
        )
        self.restart_trial_button.setEnabled(can_retry)
        self.choose_other_button.setEnabled(can_retry)

        self._refresh_tara_label()