"""
esp32_controller.py

High-level interface to the ESP32, used directly by the UI layer.

Combines protocol.py (message format) and serial_manager.py (transport)
to expose simple, purpose-named methods: ping(), home(), move_manual(),
send_trajectory(), run(), pause(), resume(), abort().

Synchronization model:
    - Fast, deterministic commands (PING, HOME, MANUAL, trajectory
      transfer) block the caller with a timeout and return a clear
      result. This keeps UI code simple: no callbacks needed for these.
    - Long-running or inherently asynchronous events (trajectory
      execution finishing, unsolicited state changes) are delivered via
      callback attributes (on_finished, on_paused, on_error, etc.),
      since the UI cannot reasonably block waiting for them.

Thread safety:
    All public methods may be called from the UI thread. Internally,
    a single pending-response slot is used per call; concurrent calls
    from multiple threads are serialized via a lock, since the ESP32
    itself only processes one command at a time.
"""

import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from src.communication import protocol
from src.communication.protocol import HomeLimits, ParsedResponse, Position, TrajectoryPoint, TrajectoryStepDelta
from src.communication.serial_manager import SerialManager, SerialManagerError


class ESP32ControllerError(Exception):
    """Base exception for all ESP32Controller failures."""
    pass


class TimeoutWaitingForResponseError(ESP32ControllerError):
    """Raised when an expected response did not arrive within the timeout."""
    pass


class DeviceReportedError(ESP32ControllerError):
    """Raised when the ESP32 responded with an ERROR message."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"ESP32 reported error [{code}]: {message}")


@dataclass
class TrajectoryTransferResult:
    """Outcome of sending a full trajectory to the ESP32."""
    success: bool
    points_acknowledged: int
    error: Optional[str] = None


class ESP32Controller:
    """
    High-level controller for the gait simulator's ESP32.

    Usage:
        controller = ESP32Controller(port="/dev/ttyUSB0")
        controller.on_trajectory_finished = my_ui_callback
        controller.connect()

        if controller.ping():
            print("ESP32 is alive")

        controller.home()
        result = controller.send_trajectory(points)
        if result.success:
            controller.run()  # returns immediately; on_trajectory_finished
                               # fires later when FINISHED arrives
    """

    DEFAULT_TIMEOUT = 3.0        # seconds, for fast commands
    # Homing now includes a full 3-axis limit-mapping sweep (see
    # docs/protocol.md, Calibration Events). On real hardware this is a
    # physical motor sweep to each limit switch with no predictable
    # upper bound (confirmed on real hardware to take >1 minute, well
    # past the old 20s value tuned for the test firmware's ~4s/axis
    # simulated sweep) — so HOME waits for READY with no timeout at all
    # rather than guessing a duration.
    HOME_TIMEOUT = None

    def __init__(self, port: str, baudrate: int = 115200):
        self._serial = SerialManager(port=port, baudrate=baudrate)
        self._serial.on_line_received = self._handle_line
        self._serial.on_disconnected = self._handle_disconnected

        # --- Pending-response synchronization ---
        self._call_lock = threading.Lock()      # serializes public calls
        self._response_event = threading.Event()
        self._expected_kinds: List[str] = []     # kinds that satisfy the wait
        self._last_response: Optional[ParsedResponse] = None

        # --- Async event callbacks (set these from the UI layer) ---
        self.on_trajectory_finished: Optional[Callable[[], None]] = None
        self.on_trajectory_progress: Optional[Callable[[TrajectoryPoint], None]] = None
        self.on_paused: Optional[Callable[[], None]] = None
        self.on_resumed: Optional[Callable[[], None]] = None
        self.on_error: Optional[Callable[[str, str], None]] = None  # code, message
        self.on_disconnected: Optional[Callable[[], None]] = None

        # --- TRAJ_STATUS polling (2026-09-01, robustness backstop) ---
        # See _start_status_polling()/_status_poll_loop() below: a
        # background thread that actively asks <TRAJ_STATUS> every 1s
        # while a trajectory is RUNNING, so completion is still detected
        # even if the normal unsolicited FINISHED line is ever lost on
        # the wire. `_status_poll_generation` (guarded by
        # `_status_poll_lock`) is how a poller thread notices it has
        # been superseded/stopped without needing thread.join() (which
        # would risk a self-join deadlock when the poller itself is the
        # one detecting the terminal status) — every call to
        # _start_status_polling()/_stop_status_polling() bumps it, and
        # a running loop iteration checks it before acting.
        self._status_poll_lock = threading.Lock()
        self._status_poll_generation = 0
        self._status_poll_interval = 1.0
        # Guards on_trajectory_finished so it fires exactly once per
        # run, whichever of the two independent paths notices FINISHED
        # first: the normal unsolicited line (_handle_line) or this
        # poller (_status_poll_loop). Reset in run() only — a
        # pause()/resume() cycle is still the SAME run.
        self._finished_notify_lock = threading.Lock()
        self._finished_notified = False

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Open the serial connection to the ESP32."""
        self._serial.connect()

    def disconnect(self) -> None:
        """Close the serial connection."""
        self._serial.disconnect()

    @property
    def is_connected(self) -> bool:
        return self._serial.is_connected

    # ------------------------------------------------------------------
    # Fast, blocking commands
    # ------------------------------------------------------------------

    def ping(self, timeout: float = DEFAULT_TIMEOUT) -> bool:
        """
        Check whether the ESP32 is alive and responding.

        Returns:
            True if PONG was received within the timeout, False otherwise.
            Does not raise on timeout (a failed ping is a normal, expected
            outcome, not an exceptional one).
        """
        try:
            self._send_and_wait(
                protocol.build_ping(), expected_kinds=["PONG"], timeout=timeout
            )
            return True
        except TimeoutWaitingForResponseError:
            return False

    def home(self, timeout: Optional[float] = HOME_TIMEOUT) -> HomeLimits:
        """
        Trigger homing (calibration + limit mapping).

        Returns:
            The 4 raw-step limits reported on the "READY:xmax:ymax:amin:
            amax" response (see docs/protocol.md, "Cambio 2026-08-31
            (READY con límites)") — converting to cm/deg is
            SystemStateMachine's job, not this layer's.

        Raises:
            TimeoutWaitingForResponseError: If timeout is not None and
                READY is not received in time. With the default (None),
                this call blocks indefinitely for READY — real homing
                has no predictable duration.
            DeviceReportedError: If the ESP32 responds with ERROR.
        """
        response = self._send_and_wait(
            protocol.build_home(), expected_kinds=["READY"], timeout=timeout
        )
        return protocol.parse_home_limits(response.payload)

    def move_manual(
        self, axis: str, direction: str, steps: int, timeout: float = DEFAULT_TIMEOUT
    ) -> None:
        """
        Move a single axis manually, in raw motor steps, no bounds
        checking beyond the firmware's own physical limit switches.

        Reconciled 2026-09-02 against the real firmware: unlike the rest
        of the protocol, MANUAL has NO state requirement at all on the
        ESP32 side — it works before HOME too (see
        SystemStateMachine.can_move_manually_raw()), since the firmware
        doesn't need a calibrated origin to move a motor, only physical
        limit switches as the safety backstop. It IS non-blocking on the
        firmware: OK means the move was accepted and started, not that
        it finished — use move_manual_stop() to interrupt it, or
        GET_POSITION/all_axes_finished() indirectly (a further MANUAL
        call on a still-moving axis gets BUSY, see below).

        Raises:
            DeviceReportedError: If the ESP32 responds BUSY (the
                targeted axis hasn't finished a previous move yet) or
                ERROR (e.g. limit already pressed toward that direction).
            TimeoutWaitingForResponseError: If no response arrives.
        """
        message = protocol.build_manual_move(axis, direction, steps)
        response = self._send_and_wait(
            message, expected_kinds=["OK", "BUSY"], timeout=timeout
        )
        if response.kind == "BUSY":
            raise DeviceReportedError(
                code="BUSY", message="El eje ya está en movimiento."
            )

    def move_manual_stop(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        """
        Stop any manual move currently in progress, on any axis.
        Idempotent — safe to call even if nothing is moving (still
        responds STOPPED). See move_manual()'s docstring for why this
        exists: MANUAL is non-blocking/asynchronous on the firmware.

        Raises:
            TimeoutWaitingForResponseError: If no response arrives.
        """
        self._send_and_wait(
            protocol.build_manual_stop(), expected_kinds=["STOPPED"], timeout=timeout
        )

    def get_position(self, timeout: float = DEFAULT_TIMEOUT) -> Position:
        """
        Query the ESP32's current tracked (x, y, angle) position — a RAW
        MOTOR STEP COUNT per axis, NOT cm/deg (see docs/protocol.md,
        Consulta de Posición, "Cambio 2026-08-26" — same steps-not-real-
        units treatment as HOME's READY:xmax:ymax:amin:amax).
        Converting to cm/deg is SystemStateMachine.get_position()'s job
        (STEPS_PER_CM_Y/STEPS_PER_CM_X/STEPS_PER_DEG_ANGLE), not this
        method's — callers needing real units must go through that, not
        this raw layer directly.

        Raises:
            TimeoutWaitingForResponseError: If no POSITION response arrives.
        """
        response = self._send_and_wait(
            protocol.build_get_position(), expected_kinds=["POSITION"], timeout=timeout
        )
        return protocol.parse_position(response.payload)

    # ------------------------------------------------------------------
    # Trajectory transfer
    # ------------------------------------------------------------------

    def send_trajectory(
        self,
        deltas: List[TrajectoryStepDelta],
        timed: bool = True,
        timeout_per_point: float = DEFAULT_TIMEOUT,
    ) -> TrajectoryTransferResult:
        """
        Send a full trajectory to the ESP32, delta by delta.

        This is a blocking, multi-step exchange: TRAJ_BEGIN, then one
        TRAJ_POINT per delta (each acknowledged individually), then
        TRAJ_END. If any step fails, the transfer stops immediately and
        the failure is reported in the returned result rather than
        raising — callers should check `.success` before proceeding to
        run().

        Args:
            deltas: The wire-level step deltas to send, in order — see
                TrajectoryStepDelta in protocol.py. Callers holding
                absolute cm/deg TrajectoryPoints (CSV-loaded or
                generated) must convert first; this class has no unit-
                conversion knowledge of its own (see
                SystemStateMachine.send_trajectory(), the only real
                caller, which does that conversion).
            timed: Must match exactly whether `deltas` themselves carry
                dt_ms (see TrajectoryStepDelta.dt_ms) — announced to the
                ESP32 via TRAJ_BEGIN's `tipo` field (2026-09-01,
                "TRAJ_BEGIN con tipo", see docs/protocol.md) so it knows
                in advance whether the TRAJ_POINT lines that follow will
                have 4 fields or 3. The SAME value must later be passed
                to run() for this trajectory (see run()'s own `timed`
                parameter) — SystemStateMachine.run() does this by
                remembering the value passed here.
            timeout_per_point: Timeout, in seconds, for each individual
                ACK while sending points.

        Returns:
            A TrajectoryTransferResult describing the outcome.
        """
        try:
            self._send_and_wait(
                protocol.build_trajectory_begin(len(deltas), timed=timed),
                expected_kinds=["TRAJ_READY"],
                timeout=timeout_per_point,
            )
        except (TimeoutWaitingForResponseError, DeviceReportedError) as exc:
            return TrajectoryTransferResult(
                success=False, points_acknowledged=0, error=str(exc)
            )

        acknowledged = 0
        for index, delta in enumerate(deltas):
            try:
                self._send_and_wait(
                    protocol.build_trajectory_step_point(index, delta),
                    expected_kinds=["ACK"],
                    timeout=timeout_per_point,
                )
                acknowledged += 1
            except (TimeoutWaitingForResponseError, DeviceReportedError) as exc:
                return TrajectoryTransferResult(
                    success=False, points_acknowledged=acknowledged, error=str(exc)
                )

        try:
            self._send_and_wait(
                protocol.build_trajectory_end(),
                expected_kinds=["TRAJ_STORED"],
                timeout=timeout_per_point,
            )
        except (TimeoutWaitingForResponseError, DeviceReportedError) as exc:
            return TrajectoryTransferResult(
                success=False, points_acknowledged=acknowledged, error=str(exc)
            )

        return TrajectoryTransferResult(success=True, points_acknowledged=acknowledged)

    # ------------------------------------------------------------------
    # Execution (asynchronous — returns immediately)
    # ------------------------------------------------------------------

    def run(self, timed: bool = True, timeout: float = DEFAULT_TIMEOUT) -> None:
        """
        Start executing the previously stored trajectory.

        This call blocks only until RUNNING is confirmed (execution has
        started), NOT until it finishes. Completion is reported later
        via on_trajectory_finished.

        Args:
            timed: Must match exactly the `timed` value passed to the
                send_trajectory() call that stored this trajectory (see
                docs/protocol.md, "TRAJ_BEGIN/RUN con tipo") — the ESP32
                cross-checks this against what TRAJ_BEGIN announced and
                rejects a mismatch with ERROR:TYPE_MISMATCH.

        Raises:
            DeviceReportedError: If the ESP32 refuses to start (e.g. no
                trajectory stored, invalid state, type mismatch).
            TimeoutWaitingForResponseError: If RUNNING is not confirmed.
        """
        self._send_and_wait(
            protocol.build_run(timed=timed), expected_kinds=["RUNNING"], timeout=timeout
        )
        # FINISHED (or an ERROR) will arrive later and is handled by
        # _handle_line() as an unsolicited message -> on_trajectory_finished.
        self._finished_notified = False
        self._start_status_polling()

    def pause(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        """Pause an in-progress trajectory execution."""
        self._stop_status_polling()
        self._send_and_wait(
            protocol.build_pause(), expected_kinds=["PAUSED"], timeout=timeout
        )

    def resume(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        """Resume a paused trajectory execution."""
        self._send_and_wait(
            protocol.build_resume(), expected_kinds=["RUNNING"], timeout=timeout
        )
        self._start_status_polling()

    def abort(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        """
        Abandon a paused trajectory entirely (operator chose to restart
        the trial or run a different one, rather than resuming) and
        return to idle. Only valid while paused — the ESP32 enforces
        this and responds with an INVALID_STATE error otherwise.

        Raises:
            DeviceReportedError: If not currently paused.
            TimeoutWaitingForResponseError: If no response arrives.
        """
        self._stop_status_polling()
        self._send_and_wait(
            protocol.build_abort(), expected_kinds=["ABORTED"], timeout=timeout
        )

    # ------------------------------------------------------------------
    # Internal: TRAJ_STATUS polling (robustness backstop for FINISHED)
    # ------------------------------------------------------------------

    def _start_status_polling(self) -> None:
        """
        Spawns a new background thread that queries <TRAJ_STATUS> every
        `_status_poll_interval` seconds. Called right after RUNNING is
        confirmed by run() or resume() — see docs/protocol.md,
        "TRAJ_STATUS". Safe to call even if a previous poller is still
        winding down: bumping the generation counter makes it a no-op
        on its next check (see _status_poll_loop).
        """
        with self._status_poll_lock:
            self._status_poll_generation += 1
            generation = self._status_poll_generation
        thread = threading.Thread(
            target=self._status_poll_loop,
            args=(generation,),
            daemon=True,
            name="TrajStatusPoller",
        )
        thread.start()

    def _stop_status_polling(self) -> None:
        """
        Signals any currently running poller to stop, without blocking
        to join it (join() here could deadlock if called FROM the
        poller thread itself, e.g. when it's the one that just detected
        FINISHED/PAUSED/ABORTED — see _status_poll_loop). The loop
        checks the generation counter at each 1s wake-up and after each
        TRAJ_STATUS reply, so a superseded thread exits within at most
        one poll cycle without sending anything further.
        """
        with self._status_poll_lock:
            self._status_poll_generation += 1

    def _status_poll_loop(self, generation: int) -> None:
        """
        Runs on its own thread. Sends <TRAJ_STATUS> once per
        `_status_poll_interval`, for as long as this thread's
        `generation` is still the current one (see _start_status_polling/
        _stop_status_polling) and the ESP32 keeps reporting RUNNING.

        This is purely a REDUNDANT completion check — the normal
        unsolicited FINISHED (handled in _handle_line) is still the
        primary, fastest path; this loop exists so a run still gets
        detected as finished (within ~1s) even if that line is ever
        dropped or corrupted on the wire. Applies to every trajectory
        run through run()/resume() alike, whether TRAJ_POINT was sent
        with or without dt_ms — TRAJ_STATUS is independent of that.

        A TRAJ_STATUS exchange that times out or gets ERROR (e.g. older
        firmware that doesn't recognize the command yet) is treated as
        "try again next cycle", not a fatal condition — this loop must
        never itself raise into the caller, since nothing is waiting on
        it synchronously.
        """
        while True:
            time.sleep(self._status_poll_interval)
            with self._status_poll_lock:
                if generation != self._status_poll_generation:
                    return

            try:
                response = self._send_and_wait(
                    protocol.build_traj_status(),
                    expected_kinds=["RUNNING", "FINISHED", "PAUSED", "ABORTED"],
                    timeout=self.DEFAULT_TIMEOUT,
                )
            except ESP32ControllerError:
                # Timeout, device error (e.g. UNKNOWN_COMMAND on
                # firmware without TRAJ_STATUS yet), or a send failure —
                # just retry on the next cycle rather than giving up.
                continue

            with self._status_poll_lock:
                if generation != self._status_poll_generation:
                    return

            if response.kind == "FINISHED":
                self._notify_finished_once()
                return
            if response.kind in ("PAUSED", "ABORTED"):
                # Execution already left RUNNING through some other,
                # already-handled path (explicit pause()/abort(), which
                # each stop polling themselves) — nothing left to poll.
                return
            # response.kind == "RUNNING": keep polling.

    def _notify_finished_once(self) -> None:
        """
        Fires on_trajectory_finished at most once per run() (reset by
        run() itself, see its `_finished_notified = False`) — shared by
        both the unsolicited-FINISHED path in _handle_line and this
        poller's own FINISHED detection, so a trajectory that completes
        with an intact wire message never double-fires the callback
        just because the poller's TRAJ_STATUS reply happened to arrive
        around the same time.
        """
        with self._finished_notify_lock:
            if self._finished_notified:
                return
            self._finished_notified = True
        if self.on_trajectory_finished is not None:
            self.on_trajectory_finished()

    # ------------------------------------------------------------------
    # Internal: send-and-wait mechanism
    # ------------------------------------------------------------------

    def _send_and_wait(
        self, message: str, expected_kinds: List[str], timeout: float
    ) -> ParsedResponse:
        """
        Send a message and block until a response of one of the expected
        kinds arrives, or until an ERROR arrives, or until timeout.

        This is the single chokepoint all blocking commands go through.
        The lock ensures only one such exchange happens at a time, since
        the ESP32 is a single-threaded device that processes one command
        at a time — overlapping exchanges would corrupt each other's
        expected responses.
        """
        with self._call_lock:
            self._response_event.clear()
            self._last_response = None
            # ERROR is always an acceptable "terminating" response, in
            # addition to whatever the caller specifically expects.
            self._expected_kinds = expected_kinds + ["ERROR"]

            try:
                self._serial.send_line(message)
            except SerialManagerError as exc:
                raise ESP32ControllerError(f"Failed to send command: {exc}") from exc

            received_in_time = self._response_event.wait(timeout=timeout)

            if not received_in_time:
                raise TimeoutWaitingForResponseError(
                    f"No response received for '{message.strip()}' "
                    f"within {timeout}s."
                )

            response = self._last_response
            if response.kind == "ERROR":
                code, _, msg = (response.payload or "").partition(":")
                raise DeviceReportedError(code=code, message=msg)

            return response

    # ------------------------------------------------------------------
    # Internal: line dispatch (runs on SerialManager's background thread)
    # ------------------------------------------------------------------

    def _handle_line(self, line: str) -> None:
        """
        Called by SerialManager for every line received from the ESP32.

        Runs on the background read thread. Splits handling into two
        paths: lines that satisfy a pending _send_and_wait() call, and
        unsolicited lines (FINISHED after RUN returned, or asynchronous
        errors) that are dispatched to the appropriate callback instead.
        """
        response = protocol.parse_response(line)

        if response.kind in self._expected_kinds:
            self._last_response = response
            self._response_event.set()
            return

        # Unsolicited messages: not what any pending call is waiting for.
        if response.kind == "FINISHED":
            self._stop_status_polling()
            self._notify_finished_once()
        elif response.kind == "TRAJ_PROGRESS":
            if self.on_trajectory_progress is not None:
                point = protocol.parse_trajectory_progress(response.payload)
                self.on_trajectory_progress(point)
        elif response.kind == "PAUSED":
            if self.on_paused is not None:
                self.on_paused()
        elif response.kind == "RUNNING":
            if self.on_resumed is not None:
                self.on_resumed()
        elif response.kind == "ERROR":
            code, _, msg = (response.payload or "").partition(":")
            if self.on_error is not None:
                self.on_error(code, msg)
        # UNKNOWN or other kinds: silently ignored here; a future
        # logging module (see project roadmap) should record these
        # rather than the controller printing directly.

    def _handle_disconnected(self) -> None:
        """Called by SerialManager if the connection is unexpectedly lost."""
        self._stop_status_polling()
        self._response_event.set()  # unblock any pending wait immediately
        if self.on_disconnected is not None:
            self.on_disconnected()