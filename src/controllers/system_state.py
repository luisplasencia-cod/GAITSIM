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
from typing import Callable, Dict, List, Optional

from src.communication.esp32_controller import (
    ESP32Controller,
    TrajectoryTransferResult,
)
from src.communication.protocol import Position, TrajectoryPoint, TrajectoryStepDelta
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
    the ESP32 itself reports `LIMANGMIN`/`LIMANGMAX` as signed step
    counts already relative to horizontal = step 0 (e.g. -5000 at the
    lower switch, 5400 at the upper one) instead of the RPi applying a
    fixed offset afterward (see docs/protocol.md, Calibration Events,
    "Cambio 2026-08-26 (eje angular)", and _build_calibration_space
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

        self.on_state_changed: Optional[Callable[[SystemState], None]] = None
        self.on_trajectory_finished: Optional[Callable[[], None]] = None
        self.on_trajectory_progress: Optional[Callable[[TrajectoryPoint], None]] = None
        self.on_calibration_limit: Optional[Callable[[str, str, Optional[float]], None]] = None

        # Result of the most recently completed HOME's limit-mapping
        # sweep (see CalibrationSpace) — None until the first HOME
        # succeeds this session. Accumulated per-axis in
        # _calibration_data while HOMING, then reduced into
        # last_calibration_space once all 3 axes report both limits.
        self.last_calibration_space: Optional[CalibrationSpace] = None
        self._calibration_data: Dict[str, Dict[str, float]] = {}

        # Subscribe to the controller's asynchronous events so the
        # state machine reacts to things that happen without the UI
        # initiating them (trajectory finishing, unexpected errors).
        self._controller.on_trajectory_finished = self._on_finished
        self._controller.on_trajectory_progress = self._on_progress
        self._controller.on_error = self._on_device_error
        self._controller.on_disconnected = self._on_disconnected
        self._controller.on_calibration_limit = self._on_calibration_limit

    @property
    def state(self) -> SystemState:
        """The current system state."""
        return self._state

    # ------------------------------------------------------------------
    # Query methods — used by the UI to decide what to show/enable
    # ------------------------------------------------------------------

    def can_move_manually(self) -> bool:
        """True only when idle (not homing, not running, not receiving)."""
        return self._state == SystemState.IDLE

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
        self._calibration_data = {}
        try:
            self._controller.home()
        except Exception:
            self._set_state(previous_state)
            raise
        self._homed = True
        self.last_calibration_space = self._build_calibration_space()
        self._set_state(SystemState.IDLE)

    def move_manual(self, axis: str, direction: str, steps: int) -> None:
        """
        Move a single axis manually. Only allowed while IDLE.

        Raises:
            InvalidTransitionError: If not currently idle.
            Any exception ESP32Controller.move_manual() may raise.
        """
        if not self.can_move_manually():
            raise InvalidTransitionError(
                f"Cannot move manually while in state {self._state.name}."
            )
        # Manual moves are quick and do not warrant their own transient
        # state; the system remains IDLE before and after.
        self._controller.move_manual(axis, direction, steps)

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

    def send_trajectory(self, points: List[TrajectoryPoint]) -> TrajectoryTransferResult:
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

        Raises:
            InvalidTransitionError: If not currently idle.
        """
        if not self.can_send_trajectory():
            raise InvalidTransitionError(
                f"Cannot send trajectory while in state {self._state.name}."
            )
        self._set_state(SystemState.RECEIVING_TRAJECTORY)
        deltas = self._points_to_step_deltas(points)
        result = self._controller.send_trajectory(deltas)
        self._set_state(SystemState.IDLE)
        return result

    def _points_to_step_deltas(self, points: List[TrajectoryPoint]) -> List[TrajectoryStepDelta]:
        """
        Converts an absolute cm/deg/seconds TrajectoryPoint list into
        the signed step-delta list TRAJ_POINT carries on the wire (see
        docs/protocol.md, Comandos de Transferencia de Trayectoria,
        "Cambio 2026-08-26 (trayectorias en pasos)").

        Every point's delta (INCLUDING the first) is computed against
        the platform's ACTUAL current position (self.get_position()),
        never assumed to already match points[0] — this mirrors exactly
        how the firmware itself accumulates (its running total starts
        at its own tracked position when TRAJ_BEGIN arrives, see the
        test firmware's handleTrajBegin()/trajAccumXSteps). Produces
        exactly len(points) deltas — same count as before this change,
        so TRAJ_BEGIN's n_points is unaffected.

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
        for point in points:
            t_ms, x, y, a = to_steps(point.t, point.x, point.y, point.angle)
            deltas.append(TrajectoryStepDelta(
                dt_ms=t_ms - prev_t_ms,
                dx_steps=x - prev_x,
                dy_steps=y - prev_y,
                dangle_steps=a - prev_a,
            ))
            prev_t_ms, prev_x, prev_y, prev_a = t_ms, x, y, a
        return deltas

    def run(self) -> None:
        """
        Start executing the stored trajectory. Transitions to RUNNING
        immediately; the eventual transition back to IDLE happens
        automatically when FINISHED is received (see _on_finished).

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
            self._controller.run()
        except Exception:
            # run() failed to even start; state remains IDLE.
            raise
        self._set_state(SystemState.RUNNING)

    def pause(self) -> None:
        """Pause an in-progress run. Transitions RUNNING -> PAUSED."""
        if not self.can_pause():
            raise InvalidTransitionError(
                f"Cannot pause while in state {self._state.name}."
            )
        self._controller.pause()
        self._set_state(SystemState.PAUSED)

    def resume(self) -> None:
        """Resume a paused run. Transitions PAUSED -> RUNNING."""
        if not self.can_resume():
            raise InvalidTransitionError(
                f"Cannot resume while in state {self._state.name}."
            )
        self._controller.resume()
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
        result = self.send_trajectory(points)
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

    def _on_calibration_limit(self, axis: str, bound: str, value: Optional[float]) -> None:
        """
        Called for each LIM{AXIS}MIN/MAX event during a HOME's
        limit-mapping sweep. Accumulates the RAW, wire-level step
        counts (never cm/deg — see docs/protocol.md, Calibration
        Events) into _calibration_data. Y/X's MIN carries no argument
        (`value` is None, defaults to 0.0 — that axis's own limit
        switch IS raw step zero); MAX (all axes) and angular's MIN
        carry a signed step count as-is, since the angular axis's
        limit switches don't sit at level/horizontal — the ESP32
        itself reports steps already relative to horizontal = 0 for
        that axis (see "Cambio 2026-08-26 (eje angular)"), so this
        method does no axis-specific interpretation of its own. The
        final CalibrationSpace is built once home() confirms READY,
        not here, since a mid-sweep read could see a partially-filled
        map — that is also where the steps->cm/deg conversion happens
        (see STEPS_PER_CM_Y/STEPS_PER_CM_X/STEPS_PER_DEG_ANGLE,
        _build_calibration_space).
        """
        data = self._calibration_data.setdefault(axis, {"min": 0.0, "max": 0.0})
        bound_key = "min" if bound == "MIN" else "max"
        data[bound_key] = value if value is not None else data[bound_key]
        if self.on_calibration_limit is not None:
            self.on_calibration_limit(axis, bound, value)

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

    def _build_calibration_space(self) -> Optional[CalibrationSpace]:
        """Reduces the 3 axes accumulated in _calibration_data (raw
        motor step counts) into a CalibrationSpace in cm/deg, or None
        if the sweep didn't report all 3 (e.g. an older firmware that
        only sends bare READY). All 3 axes are simply divided by their
        STEPS_PER_CM_*/STEPS_PER_DEG_ANGLE constant — no offset is
        applied here for the angular axis: since 2026-08-26 the ESP32
        itself reports LIMANGMIN/LIMANGMAX as signed steps already
        relative to horizontal = step 0 (see docs/protocol.md,
        Calibration Events, "Cambio 2026-08-26 (eje angular)"), so
        `a["min"]` is typically negative and `a["max"]` positive
        already."""
        axes = ("Y", "X", "A")
        if not all(axis in self._calibration_data for axis in axes):
            return None
        y, x, a = (self._calibration_data[axis] for axis in axes)
        return CalibrationSpace(
            y_min=y["min"] / self.STEPS_PER_CM_Y,
            y_max=y["max"] / self.STEPS_PER_CM_Y,
            x_min=x["min"] / self.STEPS_PER_CM_X,
            x_max=x["max"] / self.STEPS_PER_CM_X,
            angle_min=a["min"] / self.STEPS_PER_DEG_ANGLE,
            angle_max=a["max"] / self.STEPS_PER_DEG_ANGLE,
        )

    def _on_device_error(self, code: str, message: str) -> None:
        """
        Called when the ESP32 reports an unsolicited ERROR (not tied to
        a specific pending call — e.g. a fault during RUNNING).

        Conservative choice: on any device-reported error, fall back to
        IDLE rather than guessing a more specific recovery state. This
        may be refined later (e.g. a dedicated FAULT state) once real
        failure modes are better understood from the definitive firmware.
        """
        self._set_state(SystemState.IDLE)

    def _on_disconnected(self) -> None:
        """Called when the serial connection is unexpectedly lost."""
        self._homed = False  # a fresh connect requires a fresh HOME
        self._set_state(SystemState.DISCONNECTED)

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