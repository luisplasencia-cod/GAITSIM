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
    # docs/protocol.md, Calibration Events) — the test firmware spends
    # ~4s per axis (~12s total); real margin on top of that.
    HOME_TIMEOUT = 20.0

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

    def home(self, timeout: float = HOME_TIMEOUT) -> HomeLimits:
        """
        Trigger homing (calibration + limit mapping).

        Returns:
            The 4 raw-step limits reported on the "READY:xmax:ymax:amin:
            amax" response (see docs/protocol.md, "Cambio 2026-08-31
            (READY con límites)") — converting to cm/deg is
            SystemStateMachine's job, not this layer's.

        Raises:
            TimeoutWaitingForResponseError: If READY is not received in time.
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
        Move a single axis manually. Only valid when the system is idle
        (not homing, not running) — the ESP32 itself enforces this and
        will respond with an INVALID_STATE error otherwise.

        Raises:
            DeviceReportedError: If the move is rejected (e.g. wrong
                state, limit reached).
            TimeoutWaitingForResponseError: If no response arrives.
        """
        message = protocol.build_manual_move(axis, direction, steps)
        self._send_and_wait(message, expected_kinds=["OK"], timeout=timeout)

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
        self, deltas: List[TrajectoryStepDelta], timeout_per_point: float = DEFAULT_TIMEOUT
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
            timeout_per_point: Timeout, in seconds, for each individual
                ACK while sending points.

        Returns:
            A TrajectoryTransferResult describing the outcome.
        """
        try:
            self._send_and_wait(
                protocol.build_trajectory_begin(len(deltas)),
                expected_kinds=["TRAJ_READY"],
                timeout=timeout_per_point,
            )
        except (TimeoutWaitingForResponseError, DeviceReportedError) as exc:
            return TrajectoryTransferResult(
                success=False, points_acknowledged=0, error=str(exc)
            )

        acknowledged = 0
        for delta in deltas:
            try:
                self._send_and_wait(
                    protocol.build_trajectory_step_point(delta),
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

    def run(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        """
        Start executing the previously stored trajectory.

        This call blocks only until RUNNING is confirmed (execution has
        started), NOT until it finishes. Completion is reported later
        via on_trajectory_finished.

        Raises:
            DeviceReportedError: If the ESP32 refuses to start (e.g. no
                trajectory stored, invalid state).
            TimeoutWaitingForResponseError: If RUNNING is not confirmed.
        """
        self._send_and_wait(
            protocol.build_run(), expected_kinds=["RUNNING"], timeout=timeout
        )
        # FINISHED (or an ERROR) will arrive later and is handled by
        # _handle_line() as an unsolicited message -> on_trajectory_finished.

    def pause(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        """Pause an in-progress trajectory execution."""
        self._send_and_wait(
            protocol.build_pause(), expected_kinds=["PAUSED"], timeout=timeout
        )

    def resume(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        """Resume a paused trajectory execution."""
        self._send_and_wait(
            protocol.build_resume(), expected_kinds=["RUNNING"], timeout=timeout
        )

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
        self._send_and_wait(
            protocol.build_abort(), expected_kinds=["ABORTED"], timeout=timeout
        )

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
            if self.on_trajectory_finished is not None:
                self.on_trajectory_finished()
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
        self._response_event.set()  # unblock any pending wait immediately
        if self.on_disconnected is not None:
            self.on_disconnected()