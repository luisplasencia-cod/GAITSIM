"""
system_state.py

Single source of truth for the simulator's operational state on the
Raspberry Pi side. Wraps an ESP32Controller, subscribing to its
callbacks to stay in sync reactively (no polling), and exposes simple
query methods the UI layer uses to decide what actions are currently
allowed (e.g. whether manual axis movement should be enabled).

This module owns state TRANSITIONS and BUSINESS RULES (e.g. "no manual
movement while running"). It does NOT talk to the serial port directly
and does NOT know the wire protocol — that remains ESP32Controller's
and protocol.py's responsibility respectively.
"""

import logging
import threading
from dataclasses import dataclass
from enum import Enum, auto
from typing import Callable, List, Optional

from src.communication.esp32_controller import (
    DeviceReportedError,
    ESP32Controller,
    TrajectoryTransferResult,
)
from src.communication.protocol import HomeLimits, Position, TrajectoryPoint, TrajectoryStepDelta
from src.communication.serial_manager import LOG_FILE, LineCappedFileHandler

# Logs to the SAME file as serial_manager.py's wire-level log (interleaved
# chronologically), so a state transition and the raw command that caused
# it can be read together in one timeline — added to debug a state-desync
# symptom (RPi believes PAUSED, ESP32 rejects ABORT) that only reproduces
# on real hardware. This module still doesn't know the wire FORMAT (see
# module docstring) — only where to log, not what bytes mean. Uses
# LineCappedFileHandler (not plain FileHandler) so its 500-line trim
# shares serial_manager's lock/counter instead of racing with it.
_logger = logging.getLogger("gaitsim.state")
_logger.setLevel(logging.DEBUG)
if not _logger.handlers:
    _formatter = logging.Formatter(
        "%(asctime)s.%(msecs)03d %(message)s", datefmt="%H:%M:%S"
    )
    _file_handler = LineCappedFileHandler(LOG_FILE)
    _file_handler.setFormatter(_formatter)
    _logger.addHandler(_file_handler)
    _logger.propagate = False


class SystemState(Enum):
    """
    All possible operational states of the simulator, from the
    Raspberry Pi's point of view.
    """
    DISCONNECTED = auto()          # no serial connection established yet
    IDLE = auto()                  # connected, homed, ready for commands
    HOMING = auto()                # HOME command in progress
    RECEIVING_TRAJECTORY = auto()  # send_trajectory() in progress
    RUNNING = auto()               # trajectory execution in progress
    PAUSED = auto()                # execution paused mid-run


@dataclass
class CalibrationSpace:
    """
    The available movement-space map computed from a completed HOME's
    3-axis limit-mapping sweep (see docs/protocol.md, Calibration
    Events). Units: cm for x/y, degrees for angle — same convention as
    Position.

    `y_min`/`x_min` are always 0.0 — for those axes, the min limit
    switch IS that axis's zero by definition, and the wire protocol
    itself is raw/limit-relative (see docs/protocol.md). `angle_min` is
    DIFFERENT, and this time at the wire level too (since 2026-08-26):
    the angular axis's limit switches don't sit at level/horizontal, so
    the ESP32 itself reports `angle_min`/`angle_max` (the `amin`/`amax`
    fields of HOME's `READY:xmax:ymax:amin:amax` response — see
    docs/protocol.md, "Cambio 2026-08-31 (READY con límites)") as
    signed step counts already relative to horizontal = step 0 (e.g.
    -5000 at the lower switch, 5400 at the upper one) instead of the
    RPi applying a fixed offset afterward (see _build_calibration_space
    below) — so `angle_min` ends up negative directly from the
    firmware-reported value, no RPi-side correction involved.
    """
    y_min: float
    y_max: float
    x_min: float
    x_max: float
    angle_min: float
    angle_max: float

    @property
    def y_range(self) -> float:
        return self.y_max - self.y_min

    @property
    def x_range(self) -> float:
        return self.x_max - self.x_min

    @property
    def angle_range(self) -> float:
        return self.angle_max - self.angle_min


class InvalidTransitionError(Exception):
    """Raised when an action is attempted that is not allowed in the
    current state (e.g. manual move while RUNNING)."""
    pass


class TrajectorySpeedExceededError(Exception):
    """
    Raised by _points_to_step_deltas when one or more points would
    require moving an axis faster than MAX_SPEED_X_CM_S/MAX_SPEED_Y_CM_S/
    MAX_SPEED_ANGLE_DEG_S allow. Added 2026-09-01 after a real vertical-
    axis overspeed (measured from logs/gaitsim.log: ~90 cm/s at the
    default 30x time scale, ~2700 cm/s at a 1x scale) — caused by the
    initial-position session (InitialPositionSession) no longer matching
    the ESP32's actual position when "Load && Send" ran, so the offset
    trajectory's own first point carried a huge, unintended correction
    delta and — being a TIMED ensayo point — that correction was executed
    at the CSV's recorded speed instead of a safe one.

    Checked for EVERY point (not just the first), but ONLY for TIMED
    sends (`timed=True`, i.e. the CSV/gait ensayo) — per Luis's explicit
    call: for UNTIMED trajectories (joystick, synchronized initial move,
    safe return, "Reiniciar Ensayo") dt_ms is never actually sent on the
    wire (see _points_to_step_deltas), it's only an RPi-side placeholder
    derived from trajectory_generator.py's assumed speed constants — the
    ESP32 firmware picks its own real execution speed for those (see
    docs/protocol.md, "Cambio 2026-08-31"), which is the teammate's
    firmware's responsibility to keep safe, not a number worth enforcing
    against here. Only the timed ensayo's dt_ms is a real commitment the
    RPi controls, so only it gets this check.
    """

    MAX_LISTED = 5

    def __init__(self, violations: List["SpeedViolation"]):
        self.violations = violations
        lines = []
        for v in violations[: self.MAX_LISTED]:
            lines.append(
                f"Punto {v.index} (t={v.t:g}s): {v.axis_label} a "
                f"{v.speed:.1f}{v.unit}/s (máx {v.limit:.1f}{v.unit}/s)"
            )
        remaining = len(violations) - self.MAX_LISTED
        if remaining > 0:
            lines.append(f"... y {remaining} violación(es) más")
        message = (
            f"{len(violations)} punto(s) exceden la velocidad máxima "
            f"segura:\n" + "\n".join(lines)
        )
        super().__init__(message)


@dataclass
class SpeedViolation:
    """One axis of one point implying a speed beyond its MAX_SPEED_*
    limit — see TrajectorySpeedExceededError."""
    index: int
    t: float
    axis_label: str
    unit: str
    speed: float
    limit: float


@dataclass
class VariabilityPointResult:
    """Outcome of one perform_variability_point() call — see that
    method's docstring for what makes a point "successful"."""
    y_real: float
    success: bool
    interrupted: bool
    error_code: Optional[str] = None


class SystemStateMachine:
    """
    Tracks and controls transitions of the simulator's operational state.

    Usage:
        controller = ESP32Controller(port="/dev/ttyUSB0")
        state_machine = SystemStateMachine(controller)
        controller.connect()

        if state_machine.can_move_manually():
            state_machine.move_manual("X", "+", 100)

        state_machine.home()
        state_machine.send_trajectory(points)
        state_machine.run()  # non-blocking; state becomes RUNNING,
                              # then IDLE automatically on FINISHED

    Notifying the UI of state changes:
        Set `on_state_changed` to a callback receiving the new
        SystemState. This fires on the ESP32Controller's background
        thread — UI code must marshal back to the main/UI thread itself
        (this will be addressed explicitly when we build the UI layer).
    """

    def __init__(self, esp32_controller: ESP32Controller):
        self._controller = esp32_controller
        self._state = SystemState.DISCONNECTED
        self._lock = threading.Lock()
        # True once HOME has succeeded this session — see can_home().
        self._homed = False
        # Whether the trajectory most recently transferred via
        # send_trajectory() was TIMED (see that method's `timed` param)
        # — remembered here so run() can pass the SAME value on to
        # ESP32Controller.run(), which announces it to the ESP32 as
        # RUN's own `tipo` (2026-09-01, "TRAJ_BEGIN/RUN con tipo", see
        # docs/protocol.md). Always set by send_trajectory() right
        # before the RUN that actually executes it (send_trajectory ->
        # run() is always the very next call for a given trajectory —
        # the state machine won't allow another send_trajectory() while
        # RUNNING/PAUSED), so it's never stale at the point run() reads
        # it. Default True only matters if run() were ever (incorrectly)
        # called with nothing transferred first — the ESP32 would reject
        # that anyway (no trajectory stored).
        self._last_trajectory_timed = True

        # Max per-axis speed reached by the most recent send_trajectory()
        # call's points, computed in _points_to_step_deltas regardless of
        # whether that call succeeded or raised TrajectorySpeedExceededError
        # — read by TrajectoryScreen to show a "Vel. máx" readout right
        # after Load && Send (see docs/protocol.md history above,
        # TrajectorySpeedExceededError). (x_cm_s, y_cm_s, angle_deg_s).
        self._last_trajectory_max_speeds = (0.0, 0.0, 0.0)

        self.on_state_changed: Optional[Callable[[SystemState], None]] = None
        self.on_trajectory_finished: Optional[Callable[[], None]] = None
        self.on_trajectory_progress: Optional[Callable[[TrajectoryPoint], None]] = None
        # Fired at the END of _on_device_error()/_on_disconnected() below
        # (after this class's own state recovery already ran) — lets the
        # UI (via StateMachineBridge) know an error/disconnect happened
        # WITHOUT bypassing that recovery. See this class's own
        # `_controller.on_error`/`on_disconnected` subscription just
        # below: StateMachineBridge must subscribe to THESE two
        # attributes, never reassign `controller.on_error`/
        # `on_disconnected` directly — that single-callback slot is
        # already claimed by this class, and overwriting it silently
        # disables the state recovery entirely (2026-09-08 bug: an
        # unsolicited device error left `_state` stuck wherever it was,
        # e.g. RUNNING, instead of falling back to IDLE — every
        # can_home()/can_move_manually()/etc. then stayed locked, making
        # the app effectively unusable until restarted, which lost the
        # HOME calibration for no real reason).
        self.on_device_error: Optional[Callable[[str, str], None]] = None
        self.on_disconnected: Optional[Callable[[], None]] = None

        # Result of the most recently completed HOME's limit-mapping
        # sweep (see CalibrationSpace) — None until the first HOME
        # succeeds this session. Built directly from the HomeLimits
        # ESP32Controller.home() returns (see home() below) — since
        # 2026-08-31 that's a single "READY:xmax:ymax:amin:amax"
        # response, not events accumulated during the sweep.
        self.last_calibration_space: Optional[CalibrationSpace] = None

        # Subscribe to the controller's asynchronous events so the
        # state machine reacts to things that happen without the UI
        # initiating them (trajectory finishing, unexpected errors).
        self._controller.on_trajectory_finished = self._on_finished
        self._controller.on_trajectory_progress = self._on_progress
        self._controller.on_error = self._on_device_error
        self._controller.on_disconnected = self._on_disconnected

    @property
    def state(self) -> SystemState:
        """The current system state."""
        return self._state

    @property
    def last_trajectory_max_speeds(self) -> tuple:
        """
        (x_cm_s, y_cm_s, angle_deg_s) — the max per-axis speed implied by
        the most recently attempted send_trajectory() call's points, set
        by _points_to_step_deltas whether that call succeeded or was
        rejected by TrajectorySpeedExceededError. (0.0, 0.0, 0.0) if no
        trajectory has been sent this session.
        """
        return self._last_trajectory_max_speeds

    # ------------------------------------------------------------------
    # Query methods — used by the UI to decide what to show/enable
    # ------------------------------------------------------------------

    def can_move_manually(self) -> bool:
        """True only when idle (not homing, not running, not receiving)."""
        return self._state == SystemState.IDLE

    def can_move_manually_raw(self) -> bool:
        """
        True while connected but not yet calibrated (DISCONNECTED here
        means "no successful HOME yet this session" — see can_home()'s
        own docstring; the UI must additionally check
        controller.is_connected, same pattern as can_home()).

        Gates move_manual()/move_manual_stop() specifically — the RAW,
        un-bounded step move (added 2026-09-02, "movimiento manual antes
        de la calibración"), distinct from move_relative() (the
        calibrated joystick, gated by can_move_manually() above, which
        needs last_calibration_space and therefore can only run once
        IDLE). Deliberately does NOT also return True once IDLE: after
        HOME, the calibrated joystick is the intended manual control —
        keeping this False there avoids two overlapping manual-move
        paths coexisting in the same state. (Before 2026-09-02,
        move_manual() was gated by can_move_manually() too, i.e.
        IDLE-only, but no UI ever actually called it — this reassigns
        it to the new pre-calibration use case instead of leaving it
        dead code; see tests/manual_test_state_machine.py, updated the
        same day.)

        Safe pre-calibration because the real firmware's MANUAL command
        has no HOME/state requirement of its own (see
        ESP32Controller.move_manual()) — only the physical limit
        switches back it up, same as after calibration.
        """
        return self._state == SystemState.DISCONNECTED

    def can_home(self) -> bool:
        """
        Homing is only allowed ONCE per session — right after
        connecting, before the first successful HOME. Re-homing is
        deliberately disallowed afterward: it would reset the (0, 0, 0)
        reference origin, invalidating any initial position already
        recorded this session and the safety floor
        safe_return_to_position() relies on (see that method) — those
        are only meaningful relative to a single, stable HOME origin.
        """
        return self._state in (SystemState.DISCONNECTED, SystemState.IDLE) and not self._homed

    def can_go_to_position(self) -> bool:
        """True only when idle — same rule as manual movement."""
        return self._state == SystemState.IDLE

    def can_send_trajectory(self) -> bool:
        return self._state == SystemState.IDLE

    def can_run(self) -> bool:
        return self._state == SystemState.IDLE

    def can_pause(self) -> bool:
        return self._state == SystemState.RUNNING

    def can_resume(self) -> bool:
        return self._state == SystemState.PAUSED

    def can_abort(self) -> bool:
        """True only when paused — abandoning a run mid-execution
        (RUNNING) isn't offered; only a paused run can be abandoned."""
        return self._state == SystemState.PAUSED

    # ------------------------------------------------------------------
    # Actions — thin wrappers around ESP32Controller that also manage
    # the local state transition
    # ------------------------------------------------------------------

    def home(self) -> None:
        """
        Trigger homing. Transitions HOMING -> IDLE on success.

        Raises:
            InvalidTransitionError: If not currently allowed (e.g.
                already running).
            Any exception ESP32Controller.home() may raise (timeout,
                device-reported error) — the state is rolled back to
                its previous value in that case.
        """
        if not self.can_home():
            raise InvalidTransitionError(
                f"Cannot home while in state {self._state.name}."
            )
        previous_state = self._state
        self._set_state(SystemState.HOMING)
        try:
            limits = self._controller.home()
        except Exception:
            self._set_state(previous_state)
            raise
        self._homed = True
        self.last_calibration_space = self._build_calibration_space(limits)
        self._set_state(SystemState.IDLE)

    def move_manual(self, axis: str, direction: str, steps: int) -> None:
        """
        Move a single axis by a raw, un-bounded step count — only
        allowed pre-calibration (see can_move_manually_raw()). Not to be
        confused with move_relative() (the calibrated joystick, in real
        units, gated by can_move_manually()/IDLE instead).

        Raises:
            InvalidTransitionError: If can_move_manually_raw() is False.
            Any exception ESP32Controller.move_manual() may raise (e.g.
                DeviceReportedError if the targeted axis is still moving
                from a previous call, or a limit switch is pressed).
        """
        if not self.can_move_manually_raw():
            raise InvalidTransitionError(
                f"Cannot move manually (raw) while in state {self._state.name}."
            )
        # Manual moves are quick (and non-blocking on the firmware — see
        # ESP32Controller.move_manual()) and do not warrant their own
        # transient state; the system remains in the same state before
        # and after.
        self._controller.move_manual(axis, direction, steps)

    def move_manual_stop(self) -> None:
        """
        Stop any in-progress raw manual move (see move_manual()) on any
        axis. Same gate as move_manual() itself — this control only
        exists for the pre-calibration raw movement path.

        Raises:
            InvalidTransitionError: If can_move_manually_raw() is False.
            Any exception ESP32Controller.move_manual_stop() may raise.
        """
        if not self.can_move_manually_raw():
            raise InvalidTransitionError(
                f"Cannot stop manual movement while in state {self._state.name}."
            )
        self._controller.move_manual_stop()

    # Tolerance (degrees) for comparing an angle against
    # ANGLE_REFERENCE_DEG — floats, never compared with bare equality.
    # Widened from 1e-3 (effectively "exact match only") to +/-2.0 per
    # Luis's explicit request: the X buttons were only ever enabling at
    # angle == 0 dead-on, which was impractical in practice (small
    # accumulated float drift from repeated moves, or a slightly-off
    # rotation) — a +/-2 deg window around the reference is still safe
    # enough mechanically to allow X travel.
    # Public: connection_screen.py's _refresh_controls compares its own
    # locally-tracked position against this same constant to decide
    # whether to enable the manual X buttons (see move_relative below).
    ANGLE_REFERENCE_TOLERANCE_DEG = 2.0

    def move_relative(self, axis: str, direction: str, amount: float) -> None:
        """
        Move a single axis (X, Y, or A) by a relative amount in real
        units (cm for X/Y, degrees for A), from wherever the system
        currently is. Only allowed while IDLE, same rule as
        move_manual().

        Unlike the old instantaneous MOVE_REL-based implementation,
        this generates a smooth single-axis trajectory (the other 2
        axes held fixed at their current value) via
        trajectory_generator.generate_synchronized_trajectory(), sent
        and run through the same TRAJ_BEGIN/TRAJ_POINT/TRAJ_END + RUN
        protocol as any other trajectory — so manual jogging is no
        longer a teleport and gets TRAJ_PROGRESS visualization for
        free, same as safe_return_to_position(). Blocks until the move
        physically completes, same external contract as the old
        implementation.

        Per explicit product decision, this does NOT apply the
        floor_y/prosthesis safety envelope (that only protects
        automatic repositioning between trials, not free-form manual
        jogging) — but DOES still enforce the X-axis/reference-angle
        mechanical rule: attempting an X move while off-reference
        raises rather than silently moving. The UI is expected to
        disable the X buttons ahead of time by comparing its own
        locally-tracked position's angle against ANGLE_REFERENCE_DEG
        (see connection_screen.py's _refresh_controls) rather than
        querying this method's check live — this check here is the
        defense-in-depth backstop, not the primary gate the operator
        sees.

        Raises:
            InvalidTransitionError: If not currently idle, or if axis
                is "X" and the current angle is not at
                ANGLE_REFERENCE_DEG.
            RuntimeError: If no CalibrationSpace is available yet (see
                safe_return_to_position for the same requirement).
            trajectory_generator.PositionOutOfRangeError: If the
                resulting target would fall outside the calibrated
                movement space.
            Any exception send_trajectory()/run() may raise.
        """
        if not self.can_move_manually():
            raise InvalidTransitionError(
                f"Cannot move while in state {self._state.name}."
            )
        if self.last_calibration_space is None:
            raise RuntimeError(
                "No hay datos de calibración disponibles; no se puede "
                "generar una trayectoria de movimiento manual."
            )

        current = self.get_position()
        if axis == "X" and abs(current.angle - self.ANGLE_REFERENCE_DEG) > self.ANGLE_REFERENCE_TOLERANCE_DEG:
            raise InvalidTransitionError(
                f"No se puede mover X mientras el ángulo ({current.angle:g}°) "
                f"no esté en la referencia ({self.ANGLE_REFERENCE_DEG:g}°)."
            )

        delta = amount if direction == "+" else -amount
        target = Position(
            x=current.x + delta if axis == "X" else current.x,
            y=current.y + delta if axis == "Y" else current.y,
            angle=current.angle + delta if axis == "A" else current.angle,
        )

        # Deferred import: see safe_return_to_position for why this
        # can't be a module-level import (circular with CalibrationSpace).
        from src.utils import trajectory_generator

        points = trajectory_generator.generate_synchronized_trajectory(
            target, self.last_calibration_space, start=current
        )
        if not points:
            return
        self._run_trajectory_blocking(points)

    def get_position(self) -> Position:
        """
        Query the ESP32's current (x, y, angle) position, converted to
        cm/deg. A read-only query, allowed in any state.

        `ESP32Controller.get_position()` (the raw wire layer) returns
        raw motor STEP counts, not cm/deg (see docs/protocol.md,
        Consulta de Posición — same steps-not-real-units treatment
        already applied to the calibration sweep's LIM{AXIS}MAX). This
        method is the one place that conversion happens, using the
        same STEPS_PER_CM_Y/STEPS_PER_CM_X/STEPS_PER_DEG_ANGLE
        constants as _build_calibration_space — every other method in
        this class that needs the current position (move_relative,
        safe_return_to_position) MUST call this method, never
        `self._controller.get_position()` directly, or it will treat
        raw steps as if they were already cm/deg.
        """
        raw = self._controller.get_position()
        return Position(
            x=raw.x / self.STEPS_PER_CM_X,
            y=raw.y / self.STEPS_PER_CM_Y,
            angle=raw.angle / self.STEPS_PER_DEG_ANGLE,
        )

    def send_trajectory(
        self, points: List[TrajectoryPoint], timed: bool = True
    ) -> TrajectoryTransferResult:
        """
        Transfer a trajectory to the ESP32. Transitions
        RECEIVING_TRAJECTORY -> IDLE regardless of success or failure
        (the caller must check the returned result).

        `points` is absolute cm/deg/seconds, same as always (CSV-loaded
        or generated) — this is the ONE place that converts it into the
        signed step-delta wire format TRAJ_POINT now uses (see
        _points_to_step_deltas, docs/protocol.md, "Cambio 2026-08-26
        (trayectorias en pasos)") before handing it to
        ESP32Controller.send_trajectory(), which has no unit-conversion
        knowledge of its own. Applies to EVERY trajectory sent through
        this method — CSV ensayos, the joystick's single-axis moves
        (move_relative), the synchronized initial-position move, and
        safe_return_to_position's 5-step sequence — since they all
        funnel through here.

        Args:
            timed: True (default) sends the TIMED 4-field TRAJ_POINT
                (dt_ms included) — only the CSV/gait ensayo needs this,
                since its dt_ms values encode the actual recorded gait
                timing. False sends the UNTIMED 3-field form (no
                dt_ms), letting the ESP32 pick its own speed — used for
                every other trajectory (see docs/protocol.md, "Cambio
                2026-08-31 (TRAJ_POINT sin tiempo)"): those don't
                represent real gait timing, only placeholder speed
                constants (see trajectory_generator.py), so there is
                nothing meaningful to send. _run_trajectory_blocking()
                (joystick moves, safe return) always passes False; only
                TrajectoryScreen's CSV "Load && Send" relies on the
                True default.

        Raises:
            InvalidTransitionError: If not currently idle.
            TrajectorySpeedExceededError: Only when `timed=True` (the
                CSV/gait ensayo) — if any point implies a per-axis speed
                beyond MAX_SPEED_X_CM_S/MAX_SPEED_Y_CM_S/
                MAX_SPEED_ANGLE_DEG_S, nothing is transmitted (see
                _points_to_step_deltas). Never raised for `timed=False`.
        """
        if not self.can_send_trajectory():
            raise InvalidTransitionError(
                f"Cannot send trajectory while in state {self._state.name}."
            )
        self._set_state(SystemState.RECEIVING_TRAJECTORY)
        try:
            deltas = self._points_to_step_deltas(points, timed=timed)
        except TrajectorySpeedExceededError:
            # Nothing was transmitted (the check runs before any wire
            # traffic) — roll back exactly like a failed run() does.
            self._set_state(SystemState.IDLE)
            raise
        result = self._controller.send_trajectory(deltas, timed=timed)
        # Remembered for the run() that must immediately follow a
        # successful transfer — see this attribute's own docstring.
        self._last_trajectory_timed = timed
        self._set_state(SystemState.IDLE)
        return result

    def _points_to_step_deltas(
        self, points: List[TrajectoryPoint], timed: bool = True
    ) -> List[TrajectoryStepDelta]:
        """
        Converts an absolute cm/deg/seconds TrajectoryPoint list into
        the signed step-delta list TRAJ_POINT carries on the wire (see
        docs/protocol.md, Comandos de Transferencia de Trayectoria,
        "Cambio 2026-08-26 (trayectorias en pasos)" and "Cambio
        2026-08-31 (TRAJ_POINT sin tiempo)").

        `timed=False` (see send_trajectory()'s `timed` parameter) still
        computes each point's own t_ms internally (needed to drop the
        leading t=0 point below, same as the timed case), but sets each
        resulting TrajectoryStepDelta.dt_ms to None instead of the
        computed duration — build_trajectory_step_point() then emits
        the UNTIMED 3-field TRAJ_POINT for it.

        Every point's delta (INCLUDING the first one actually sent) is
        computed against the platform's ACTUAL current position
        (self.get_position()), never assumed to already match points[0]
        — this mirrors exactly how the firmware itself accumulates (its
        running total starts at its own tracked position when
        TRAJ_BEGIN arrives, see the test firmware's
        handleTrajBegin()/trajAccumXSteps).

        A leading point at t=0 (the convention every CSV/generated
        trajectory starts with — see trajectory_loader.py,
        trajectory_generator.py) is dropped rather than sent as a wire
        TRAJ_POINT: since our own baseline is also t=0 (`current`, "now"),
        that point would always produce dt_ms=0, and the definitive
        ESP32 firmware treats dt_ms<=0 as a hard failure (see
        run_trajectory() in the teammate's main.cpp — it stops the
        whole trajectory before any motion happens, not just that one
        point). Its position is still meaningful — it becomes the
        baseline the FIRST SENT point's delta is computed against,
        together with `current`, so a real mismatch between "where the
        trajectory assumes it starts" and "where the ESP32 actually is"
        is still captured in that first sent delta, exactly as before;
        only the always-zero-duration entry itself is skipped. Produces
        len(points) deltas normally, or len(points) - 1 when points[0].t
        == 0.0 — TRAJ_BEGIN's n_points (computed downstream from
        len(deltas)) reflects this automatically.

        Also enforces MAX_SPEED_X_CM_S/MAX_SPEED_Y_CM_S/
        MAX_SPEED_ANGLE_DEG_S on every point (including the first sent
        one, computed against `current` same as above) — but ONLY when
        `timed=True` (the CSV/gait ensayo); see TrajectorySpeedExceededError's
        docstring for why untimed sends are exempt. Raises
        TrajectorySpeedExceededError, listing every offending point, if
        any axis of any point implies a speed beyond its limit. This is
        the single choke point for that check since it's the one place
        that already computes every point's dx/dy/dangle/dt_ms
        regardless of trajectory type. self._last_trajectory_max_speeds
        is still updated for BOTH timed and untimed sends (informational
        only for untimed — see TrajectoryScreen's readout), and BEFORE
        raising, so a rejected trajectory's speed is still visible to
        the caller.

        Rounds each point's ABSOLUTE step position first, then takes
        the difference between consecutive ROUNDED absolute values —
        never rounds a delta independently — so rounding error never
        accumulates across a long trajectory. Same technique the
        teammate's own motion-data generation script uses for the
        definitive firmware (round each absolute x/y/angle to steps
        first, then diff consecutive rounded values for dx/dy/dk).
        """
        if not points:
            return []
        if points[0].t == 0.0:
            points = points[1:]
        if not points:
            return []

        def to_steps(t_sec: float, x_cm: float, y_cm: float, angle_deg: float):
            return (
                round(t_sec * 1000),
                round(x_cm * self.STEPS_PER_CM_X),
                round(y_cm * self.STEPS_PER_CM_Y),
                round(angle_deg * self.STEPS_PER_DEG_ANGLE),
            )

        current = self.get_position()
        prev_t_ms, prev_x, prev_y, prev_a = to_steps(0.0, current.x, current.y, current.angle)

        deltas = []
        violations: List[SpeedViolation] = []
        max_speed_x = max_speed_y = max_speed_angle = 0.0
        for point in points:
            t_ms, x, y, a = to_steps(point.t, point.x, point.y, point.angle)
            dt_ms = t_ms - prev_t_ms
            dx_steps, dy_steps, dangle_steps = x - prev_x, y - prev_y, a - prev_a
            deltas.append(TrajectoryStepDelta(
                dt_ms=dt_ms if timed else None,
                dx_steps=dx_steps,
                dy_steps=dy_steps,
                dangle_steps=dangle_steps,
            ))
            # dt_ms<=0 is a separate, pre-existing concern (see the
            # docstring above on the dropped t=0 baseline) — not this
            # check's job, and dividing by it here would only raise
            # ZeroDivisionError/produce a bogus infinite speed.
            if dt_ms > 0:
                dt_s = dt_ms / 1000.0
                speed_x = abs(dx_steps) / self.STEPS_PER_CM_X / dt_s
                speed_y = abs(dy_steps) / self.STEPS_PER_CM_Y / dt_s
                speed_angle = abs(dangle_steps) / self.STEPS_PER_DEG_ANGLE / dt_s
                max_speed_x = max(max_speed_x, speed_x)
                max_speed_y = max(max_speed_y, speed_y)
                max_speed_angle = max(max_speed_angle, speed_angle)
                # Enforced ONLY for TIMED sends (the CSV/gait ensayo).
                # For untimed ones (joystick, posición inicial, retorno
                # seguro, Reiniciar Ensayo) dt_ms here is never sent on
                # the wire at all (see dt_ms=None below) — it's only a
                # placeholder derived from trajectory_generator.py's
                # assumed speed constants, not a real commitment. The
                # ESP32 picks its own actual speed for those (per
                # docs/protocol.md, "Cambio 2026-08-31") — per Luis,
                # that's the teammate's firmware's job to keep safe, not
                # ours to second-guess with a fictional RPi-side number.
                if timed:
                    index = len(deltas) - 1
                    if speed_x > self.MAX_SPEED_X_CM_S:
                        violations.append(SpeedViolation(
                            index, point.t, "X", "cm", speed_x, self.MAX_SPEED_X_CM_S,
                        ))
                    if speed_y > self.MAX_SPEED_Y_CM_S:
                        violations.append(SpeedViolation(
                            index, point.t, "Y", "cm", speed_y, self.MAX_SPEED_Y_CM_S,
                        ))
                    if speed_angle > self.MAX_SPEED_ANGLE_DEG_S:
                        violations.append(SpeedViolation(
                            index, point.t, "Ángulo", "°", speed_angle,
                            self.MAX_SPEED_ANGLE_DEG_S,
                        ))
            prev_t_ms, prev_x, prev_y, prev_a = t_ms, x, y, a

        self._last_trajectory_max_speeds = (max_speed_x, max_speed_y, max_speed_angle)
        if violations:
            raise TrajectorySpeedExceededError(violations)
        return deltas

    def run(self) -> None:
        """
        Start executing the stored trajectory. Transitions to RUNNING
        immediately; the eventual transition back to IDLE happens
        automatically when FINISHED is received (see _on_finished).

        Passes `_last_trajectory_timed` (set by the send_trajectory()
        call that must have immediately preceded this one) down to
        ESP32Controller.run() as its own `timed` argument — the ESP32
        cross-checks this against what TRAJ_BEGIN announced for the
        same trajectory (see docs/protocol.md, "TRAJ_BEGIN/RUN con
        tipo") and rejects a mismatch with ERROR:TYPE_MISMATCH.

        Raises:
            InvalidTransitionError: If not currently idle.
            Any exception ESP32Controller.run() may raise (in which
                case the state is rolled back).
        """
        if not self.can_run():
            raise InvalidTransitionError(
                f"Cannot run while in state {self._state.name}."
            )
        try:
            self._controller.run(timed=self._last_trajectory_timed)
        except Exception:
            # run() failed to even start; state remains IDLE.
            raise
        self._set_state(SystemState.RUNNING)

    def pause(self) -> None:
        """
        Pause an in-progress run. Transitions RUNNING -> PAUSED.

        Fixed 2026-09-10: if the ESP32 rejects PAUSE with ERROR (e.g. it
        silently reset/rebooted mid-run — see logs/gaitsim.log incident
        this session, position jumping straight to 0:0:0 with no
        FINISHED/ABORTED), `_state` used to stay stuck at RUNNING
        forever: can_pause() kept returning True (so retrying just
        failed the same way) while can_abort() stayed False (requires
        PAUSED) — a total UI lockout needing an app/ESP32 restart,
        because this failure path never went through _on_device_error()
        (that recovery is only wired to the controller's *unsolicited*
        ERROR callback — see that method's docstring — and an ERROR
        arriving as PAUSE's own response is consumed as PAUSE's expected
        reply, never reaching that path). Now explicitly routes a
        rejected PAUSE through the same IDLE-fallback recovery before
        re-raising, so the caller's existing failure-message UI still
        shows the raw error too.
        """
        if not self.can_pause():
            raise InvalidTransitionError(
                f"Cannot pause while in state {self._state.name}."
            )
        try:
            self._controller.pause()
        except DeviceReportedError as exc:
            self._on_device_error(exc.code, exc.message)
            raise
        self._set_state(SystemState.PAUSED)

    def resume(self) -> None:
        """
        Resume a paused run. Transitions PAUSED -> RUNNING.

        Same rejected-command recovery as pause() (see its docstring,
        2026-09-10 fix) — a rejected RESUME falls back to IDLE instead
        of leaving `_state` stuck at PAUSED with no way out.
        """
        if not self.can_resume():
            raise InvalidTransitionError(
                f"Cannot resume while in state {self._state.name}."
            )
        try:
            self._controller.resume()
        except DeviceReportedError as exc:
            self._on_device_error(exc.code, exc.message)
            raise
        self._set_state(SystemState.RUNNING)

    def abort(self) -> None:
        """
        Abandon a paused run entirely (operator observed a mechanical
        fault and chose to restart the trial or run a different one,
        rather than resuming). Transitions PAUSED -> IDLE, discarding
        the rest of the trajectory. Distinct from a hypothetical
        "stop-and-resume" — RESUME is no longer possible after this.

        Raises:
            InvalidTransitionError: If not currently paused.
        """
        _logger.debug("abort() called, RPi-side state=%s", self._state.name)
        if not self.can_abort():
            _logger.debug("abort() REJECTED locally (not paused per RPi state)")
            raise InvalidTransitionError(
                f"Cannot abort while in state {self._state.name}."
            )
        try:
            self._controller.abort()
        except DeviceReportedError as exc:
            # Same rejected-command recovery as pause()/resume() (see
            # pause()'s docstring, 2026-09-10 fix) — without this, a
            # rejected ABORT left `_state` stuck at PAUSED with every
            # action gated on RUNNING/IDLE/PAUSED locked out.
            _logger.debug("abort() FAILED: ESP32 rejected it: %r", exc)
            self._on_device_error(exc.code, exc.message)
            raise
        except Exception as exc:
            _logger.debug("abort() FAILED: ESP32 rejected it: %r", exc)
            raise
        self._set_state(SystemState.IDLE)
        _logger.debug("abort() succeeded, ESP32 confirmed ABORTED")

    # ------------------------------------------------------------------
    # Safe repositioning between trials
    # ------------------------------------------------------------------

    # Vertical safety margin (cm) added above the floor before any
    # horizontal/angular movement during safe_return_to_position().
    Y_LIFT_MARGIN_CM = 5.0

    # Angle used as a neutral reference orientation while traversing
    # (see safe_return_to_position, steps 2/3/4 below). NOT necessarily
    # literal 0 degrees — once the real hardware's limit switches are
    # calibrated this may need to be a different reference value. Kept
    # as one named constant, not a literal, specifically so it is a
    # single obvious place to change later rather than a magic number
    # buried in the movement sequence.
    ANGLE_REFERENCE_DEG = 0.0

    def safe_return_to_position(self, target: Position, floor_y: float) -> None:
        """
        Move to `target` (a new trial's initial position) from wherever
        the system currently is, without re-homing (homing only happens
        once per session) and without ever letting Y drop below
        `floor_y` while X/angle are not yet at their target values.

        Why: between trials, a prosthesis may be mounted at the Y height
        of the trial that just concluded (`floor_y` — pass the initial
        position's y of that trial). Moving there in a way that lets Y
        sag while X/angle are still wrong risks colliding with and
        damaging it. This is a hard safety rule, not a convenience.

        Also never moves X while angle is anywhere other than
        ANGLE_REFERENCE_DEG (a second hard mechanical rule — X travel
        is only safe at that reference angle).

        Implementation: generates a single smooth trajectory
        (trajectory_generator.generate_safe_return_trajectory)
        preserving the EXACT SAME 5-step ordering as the original
        step-wise version — lift Y clear of `floor_y`, rotate to
        ANGLE_REFERENCE_DEG, move X, rotate to the target angle, THEN
        descend to `target` — and runs it through the existing
        TRAJ_BEGIN/TRAJ_POINT/TRAJ_END + RUN protocol (same as any gait
        trajectory or the initial (0,0,0)-> position move), so it gets
        TRAJ_PROGRESS live visualization for free instead of only
        visualizing the subsequent gait run. This call still BLOCKS
        until the move physically completes (same external contract as
        before) — see _run_trajectory_blocking().

        Requires `last_calibration_space` to be available (set by the
        one HOME performed this session) to compute the lift height's
        upper bound; this is always true whenever `can_go_to_position()`
        can be True (HOME must have succeeded first, since IDLE is
        otherwise unreachable).

        Only allowed while IDLE (see can_go_to_position()) — call after
        home()/abort()/a FINISHED callback, not while paused.

        Raises:
            InvalidTransitionError: If not currently idle.
            RuntimeError: If no CalibrationSpace is available (e.g. an
                older firmware whose HOME didn't report a full 3-axis
                limit-mapping sweep — see _build_calibration_space) —
                the lift height's upper bound cannot be computed
                without it.
            Any exception send_trajectory()/run() may raise.
        """
        if not self.can_go_to_position():
            raise InvalidTransitionError(
                f"Cannot reposition while in state {self._state.name}."
            )
        if self.last_calibration_space is None:
            raise RuntimeError(
                "No hay datos de calibración disponibles; no se puede "
                "calcular una trayectoria de retorno segura."
            )

        # Deferred import: trajectory_generator.py imports CalibrationSpace
        # from this module at its own top level, so importing it back up
        # here at module scope would be a circular import. Safe to import
        # locally at call time since by then both modules are fully loaded.
        from src.utils import trajectory_generator

        current = self.get_position()
        points = trajectory_generator.generate_safe_return_trajectory(
            current,
            target,
            floor_y,
            self.last_calibration_space,
            self.Y_LIFT_MARGIN_CM,
            self.ANGLE_REFERENCE_DEG,
        )
        if not points:
            return
        self._run_trajectory_blocking(points)

    def _run_trajectory_blocking(self, points: List[TrajectoryPoint]) -> None:
        """
        Send and run `points`, blocking until the ESP32 reports FINISHED
        (or the attempt fails), instead of returning as soon as RUNNING
        starts like the public run() does. Used internally by
        safe_return_to_position() so it keeps behaving like a single
        synchronous call to its callers, same as before this was
        rewritten to use a real trajectory instead of chained GOTOs.

        The existing `on_trajectory_finished` callback (the UI bridge's
        hook, e.g. for the post-run "save initial position?" prompt) is
        deliberately NOT invoked for this internal move — it is not a
        gait trajectory the operator ran, just an implementation detail
        of getting into position, and firing it would spuriously
        trigger UI logic meant for real trajectory completions. It is
        saved and restored around the wait so the real hook is intact
        for the gait run that follows. `on_trajectory_progress` is left
        untouched and keeps firing normally, so CalibrationMapWindow's
        live position marker shows this repositioning move too.

        Raises:
            InvalidTransitionError: If not currently idle (send_trajectory
                and run() each enforce this themselves).
            RuntimeError: If the trajectory transfer itself fails.
            Any exception run() may raise.
        """
        result = self.send_trajectory(points, timed=False)
        if not result.success:
            raise RuntimeError(
                f"No se pudo transferir la trayectoria de retorno: {result}"
            )

        finished = threading.Event()
        original_on_finished = self.on_trajectory_finished
        self.on_trajectory_finished = finished.set
        try:
            self.run()
            finished.wait()
        finally:
            self.on_trajectory_finished = original_on_finished

    # ------------------------------------------------------------------
    # Pre-run platform detach (contact-threshold ("tara") testing)
    # ------------------------------------------------------------------

    # Vertical clearance (cm) above the recorded tara Y the platform
    # rises to before shifting sideways — see
    # trajectory_generator.generate_detach_and_ensayo_trajectory(), the
    # only reader of these two constants (TrajectoryScreen._send_and_check()
    # passes them through when fusing a detach hop onto the ensayo).
    # Fixed for now (2026-09-14, Luis's explicit choice) rather than a
    # UI-configurable value: this is the more safety-critical of the
    # two detach margins, so it stays a named constant, same spirit as
    # Y_LIFT_MARGIN_CM above.
    DETACH_LIFT_ABOVE_TARA_CM = 1.0

    # Horizontal shift (cm, toward X=0) used to fully clear the force
    # platform's contact zone while lifted, before swinging back into
    # position. Also fixed for now, same reasoning as
    # DETACH_LIFT_ABOVE_TARA_CM.
    DETACH_X_SHIFT_CM = 1.0

    # How far the platform rises, right after a trajectory's last point
    # and as part of that same trajectory (see
    # trajectory_generator.append_end_lift), to lift off the force
    # platform once an ensayo ends. Fixed at 5cm per Luis's explicit
    # request (2026-09-21); same "not UI-configurable" treatment as
    # DETACH_LIFT_ABOVE_TARA_CM above.
    END_LIFT_CM = 5.0

    # ------------------------------------------------------------------
    # Height-variability / repeatability test matrix
    # ------------------------------------------------------------------

    def go_to_variability_point(self, tara: Position, depth_mm: float) -> Position:
        """
        Move to ONE point of the height-variability/repeatability test
        matrix (see variability_library.py and VariabilityMatrixScreen):
        return to `tara` (the basal/no-contact reference recorded for
        the loaded ensayo, see tara_library.py) via the SAME safe
        repositioning sequence used elsewhere in this app (lift clear,
        move X only at ANGLE_REFERENCE_DEG, descend — see
        Y_LIFT_MARGIN_CM/ANGLE_REFERENCE_DEG below and
        generate_safe_return_trajectory), then descend `depth_mm`
        millimeters below it (0 for the tara row itself) — see
        trajectory_generator.generate_variability_point_trajectory()
        for the actual movement.

        Plain GOTO-style blocking call (like safe_return_to_position())
        — it does NOT run the ensayo itself and does NOT decide
        success/failure of a "sample". Corrected 2026-09-18 (Luis,
        same day as the first version): the repeatability counter must
        reflect the ENSAYO's own Run completing from this point, not
        just reaching it — VariabilityMatrixScreen now tracks a
        "pending sample" after this call returns and records it only
        once the operator's subsequent Run (via TrajectoryScreen,
        exactly as it works for any other ensayo) actually finishes.
        This method itself has no opinion about that — it only gets
        the platform there.

        Returns:
            The real position reached (via GET_POSITION) once the move
            completes — the caller records this as the eventual
            sample's y_real.

        Raises:
            InvalidTransitionError: If not currently idle.
            RuntimeError: If no CalibrationSpace is available, or the
                trajectory transfer itself fails.
            PositionOutOfRangeError: If `tara.y - depth_mm/10` falls
                outside the calibrated movement space.
        """
        if not self.can_go_to_position():
            raise InvalidTransitionError(
                f"Cannot go to variability point while in state {self._state.name}."
            )
        if self.last_calibration_space is None:
            raise RuntimeError(
                "No hay datos de calibración disponibles; no se puede "
                "calcular el punto de la matriz de variabilidad."
            )

        from src.utils import trajectory_generator

        current = self.get_position()
        points = trajectory_generator.generate_variability_point_trajectory(
            current, tara, depth_mm / 10.0, self.last_calibration_space,
            self.Y_LIFT_MARGIN_CM, self.ANGLE_REFERENCE_DEG,
        )
        if points:
            self._run_trajectory_blocking(points)
        return self.get_position()

    # ------------------------------------------------------------------
    # Internal: reactions to asynchronous ESP32Controller events
    # ------------------------------------------------------------------

    def _on_finished(self) -> None:
        """Called when the ESP32 reports FINISHED (trajectory complete)."""
        self._set_state(SystemState.IDLE)
        if self.on_trajectory_finished is not None:
            self.on_trajectory_finished()

    def _on_progress(self, point: TrajectoryPoint) -> None:
        """
        Called when the ESP32 reports TRAJ_PROGRESS for a single point
        during RUNNING. Does not change state.

        `point.x`/`point.y`/`point.angle` arrive as raw motor steps
        (see docs/protocol.md, Progreso de ejecución, "Cambio
        2026-08-26") — converted to cm/deg here, the one place that
        happens, before forwarding for live plotting on the UI side
        (which expects cm/deg, unchanged by this). `point.t` is
        untouched (already real seconds).
        """
        if self.on_trajectory_progress is not None:
            converted = TrajectoryPoint(
                t=point.t,
                x=point.x / self.STEPS_PER_CM_X,
                y=point.y / self.STEPS_PER_CM_Y,
                angle=point.angle / self.STEPS_PER_DEG_ANGLE,
            )
            self.on_trajectory_progress(converted)

    # Real motor/mechanism constants (2026-08-26), from the teammate's
    # definitive-firmware motion-data generation script — NOT
    # arbitrary placeholders. They come from the actual leadscrew pitch
    # (mm travelled per motor revolution), motor microstepping, and
    # angular gearbox ratio of the real rig. Needed because the ESP32
    # no longer converts LIM{AXIS}MAX to cm/deg itself — it reports raw
    # steps and the RPi does the conversion (see docs/protocol.md,
    # "Cambio 2026-08-26").
    _LEADSCREW_X_MM_PER_REV = 10
    _LEADSCREW_Y_MM_PER_REV = 5
    _X_STEPS_PER_REV = 400
    _Y_STEPS_PER_REV = 400
    _K_STEPS_PER_REV = 800
    _K_GEAR_RATIO = 50

    STEPS_PER_CM_X = (10 * _X_STEPS_PER_REV) / _LEADSCREW_X_MM_PER_REV
    STEPS_PER_CM_Y = (10 * _Y_STEPS_PER_REV) / _LEADSCREW_Y_MM_PER_REV
    STEPS_PER_DEG_ANGLE = (_K_GEAR_RATIO * _K_STEPS_PER_REV) / 360

    # Hard per-axis speed ceiling enforced on EVERY point of TIMED
    # sends only (the CSV/gait ensayo — see
    # _points_to_step_deltas/TrajectorySpeedExceededError) — added
    # 2026-09-01 after a real Y-axis overspeed incident (see that
    # exception's docstring). Deliberately NOT enforced on untimed
    # sends (joystick, posición inicial, retorno seguro, "Reiniciar
    # Ensayo") per Luis: the ESP32/teammate's firmware picks its own
    # speed for those, so an RPi-side ceiling would be checking a
    # fictional number, not a real one. Placeholder, like
    # STEPS_PER_CM_X/Y above, pending real rig calibration: derived
    # from this real ensayo's own normal (non-defective) per-point
    # speed profile, re-scaled from the 30x default time scale down to
    # a conservative floor above the 1x scale that tripped a real motor
    # stall — NOT a measured motor/driver spec. Revisit once real
    # max-speed numbers are known.
    # That floor moved from 13x to 10x on 2026-09-08 (Luis's explicit
    # choice) — i.e. these 3 values are the previous ones (15.0/3.0/35.0
    # at the 13x floor) scaled up by 13/10 = 1.3, so the SAME normal
    # per-point profile that used to require at least a 13x time-scale
    # to stay under this ceiling now only needs 10x (a faster/less-
    # stretched ensayo playback).
    # TEMPORARY (2026-09-25, Luis's explicit request, for testing): all
    # three ceilings raised to 60 so the speed gate effectively doesn't
    # block runs. Previous values: 19.5 / 3.9 / 45.5. Restore them
    # (real Y stall risk, see vertical_motor_speed_limit_investigation)
    # once testing is done.
    MAX_SPEED_X_CM_S = 60.0
    MAX_SPEED_Y_CM_S = 60.0
    MAX_SPEED_ANGLE_DEG_S = 60.0

    # Speed ceilings for the fused detach hop and the end-of-ensayo
    # lift ONLY (2026-09-25). Kept at the previous safe values while
    # MAX_SPEED_* above is temporarily raised for testing, so those
    # short moves don't become unbounded-fast.
    HOP_SPEED_X_CM_S = 19.5
    HOP_SPEED_Y_CM_S = 3.9

    def _build_calibration_space(self, limits: HomeLimits) -> CalibrationSpace:
        """Converts a HOME's raw-step HomeLimits (from
        ESP32Controller.home()'s "READY:xmax:ymax:amin:amax" response)
        into a CalibrationSpace in cm/deg. y_min/x_min are always 0.0 —
        those axes' own limit switch IS raw step zero by definition, so
        the wire payload doesn't carry them at all. No offset is applied
        for the angular axis: the ESP32 itself reports angle_min/
        angle_max as signed steps already relative to horizontal = step
        0 (see docs/protocol.md, Calibración, "Cambio 2026-08-26 (eje
        angular)"), so `limits.angle_min` is typically negative and
        `limits.angle_max` positive already."""
        return CalibrationSpace(
            y_min=0.0,
            y_max=limits.y_max / self.STEPS_PER_CM_Y,
            x_min=0.0,
            x_max=limits.x_max / self.STEPS_PER_CM_X,
            angle_min=limits.angle_min / self.STEPS_PER_DEG_ANGLE,
            angle_max=limits.angle_max / self.STEPS_PER_DEG_ANGLE,
        )

    def _on_device_error(self, code: str, message: str) -> None:
        """
        Called when the ESP32 reports an unsolicited ERROR (not tied to
        a specific pending call — e.g. a fault during RUNNING).

        Conservative choice: on any device-reported error, fall back to
        IDLE rather than guessing a more specific recovery state. This
        may be refined later (e.g. a dedicated FAULT state) once real
        failure modes are better understood from the definitive firmware.

        Deliberately does NOT touch `_homed` — none of the error codes
        currently in docs/protocol.md represent a lost HOME reference
        (only an actual dropped connection does, see _on_disconnected
        below), so can_home() correctly stays False and the operator can
        resume operating from IDLE (manual move, resend, run again)
        without recalibrating.
        """
        self._set_state(SystemState.IDLE)
        if self.on_device_error is not None:
            self.on_device_error(code, message)

    def _on_disconnected(self) -> None:
        """Called when the serial connection is unexpectedly lost."""
        self._homed = False  # a fresh connect requires a fresh HOME
        self._set_state(SystemState.DISCONNECTED)
        if self.on_disconnected is not None:
            self.on_disconnected()

    # ------------------------------------------------------------------
    # Internal: state transition with notification
    # ------------------------------------------------------------------

    def _set_state(self, new_state: SystemState) -> None:
        with self._lock:
            old_state = self._state
            self._state = new_state
        _logger.debug("state: %s -> %s", old_state.name, new_state.name)
        if self.on_state_changed is not None:
            self.on_state_changed(new_state)
            
    @property
    def controller(self) -> ESP32Controller:
        """Expose the underlying ESP32Controller for callers that need
        direct access (e.g. the UI bridge, to also observe raw device
        errors/disconnections that the state machine consumes internally)."""
        return self._controller