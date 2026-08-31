"""
protocol.py


Defines the serial communication protocol between the Raspberry Pi
(high-level controller) and the ESP32 (real-time controller).

This module has a single responsibility: convert between Python data
structures and the plain-text ASCII protocol used over the serial link.
It does NOT open ports, send bytes, or manage threads — that is the
responsibility of serial_manager.py.

Protocol overview (see docs/protocol.md for full specification):
    - RPi -> ESP32 commands are framed with literal '<' and '>' delimiters
      (e.g. "<PING>"), no trailing '\n' — the ESP32 reads from '<' to '>'
      as one command, per its own general-purpose receive function.
    - ESP32 -> RPi responses are unaffected: one per line, terminated
      with '\n' (see parse_response() below).
    - RPi is master: it sends commands, ESP32 responds with status/ACK/ERROR.
    - All errors follow the format: ERROR:<code>:<message>

Trajectory data model:
    A trajectory is a single CSV with columns: time, pos_x, pos_y, angle.
    All three motion axes share the same time base for a given cycle.
"""

from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# Command keywords (RPi -> ESP32)
# ---------------------------------------------------------------------------
# Grouped as constants (not magic strings) so that a typo becomes an
# import-time / IDE-detectable error instead of a silent runtime bug.

CMD_PING = "PING"
CMD_HOME = "HOME"

CMD_MANUAL = "MANUAL"        # MANUAL:<axis>:<direction>:<steps>

# Wire encoding for the `direction` field of MANUAL: everything
# past a command's first argument must be an integer (see docs/protocol.md,
# Manual Movement Commands) — `axis` stays a letter (the allowed
# first argument), so `direction` maps "+"/"-" to 1/0, matching the DIR
# pin-level convention already used by the definitive ESP32 firmware
# (X_POSITIVE_DIR = 1, etc.) for consistency between the two protocols.
_DIRECTION_WIRE_CODE = {"+": 1, "-": 0}

CMD_TRAJ_BEGIN = "TRAJ_BEGIN"   # TRAJ_BEGIN:<n_points>
CMD_TRAJ_POINT = "TRAJ_POINT"   # TRAJ_POINT:<t>:<x>:<y>:<angle>
CMD_TRAJ_END = "TRAJ_END"

CMD_RUN = "RUN"
CMD_PAUSE = "PAUSE"
CMD_RESUME = "RESUME"
CMD_ABORT = "ABORT"     # abandon a PAUSED trajectory entirely -> IDLE

CMD_GET_POSITION = "GET_POSITION"


# ---------------------------------------------------------------------------
# Response keywords (ESP32 -> RPi)
# ---------------------------------------------------------------------------

RESP_PONG = "PONG"
RESP_OK = "OK"
RESP_TRAJ_READY = "TRAJ_READY"
RESP_TRAJ_STORED = "TRAJ_STORED"
RESP_RUNNING = "RUNNING"
RESP_FINISHED = "FINISHED"
RESP_PAUSED = "PAUSED"
RESP_ABORTED = "ABORTED"

RESP_ACK_PREFIX = "ACK:"
RESP_ERROR_PREFIX = "ERROR:"
RESP_TRAJ_PROGRESS_PREFIX = "TRAJ_PROGRESS:"
RESP_POSITION_PREFIX = "POSITION:"

# HOME's response (2026-08-31, "Cambio 2026-08-31 (READY con límites)"):
# the ESP32 no longer streams 6 separate <LIM*MIN/MAX> events during the
# limit-mapping sweep — it reports the 4 values it doesn't already know
# to be 0 (Y/X's MIN, by definition, IS raw step zero for those axes) in
# a single READY line: "READY:xmax_steps:ymax_steps:amin_steps:amax_steps".
# All 4 are raw motor STEP counts (NOT cm/deg — see docs/protocol.md,
# "Cambio 2026-08-26"); angular's amin/amax are SIGNED, already relative
# to horizontal = step 0 (its limit switches don't sit at level), same
# convention as before. This IS a normal unframed response now — there
# is no more "responses are unframed except calibration events"
# exception (see Transport in docs/protocol.md).
RESP_READY_PREFIX = "READY:"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class TrajectoryPoint:
    """
    A single ABSOLUTE sample of a gait trajectory, in cm/cm/degrees —
    this is the type used everywhere in the app that deals with
    trajectories as real-unit positions over time: CSV loading
    (trajectory_loader.py), generation (trajectory_generator.py),
    range validation (trajectory_validator.py), and the live plot.

    Attributes:
        t: Time value, in seconds, relative to the start of the cycle.
        x: Horizontal axis position, in cm.
        y: Vertical axis position, in cm.
        angle: Sagittal rotation angle, in degrees.

    NOT what actually goes out on the wire as TRAJ_POINT since
    2026-08-26 — see TrajectoryStepDelta below for that (a delta in raw
    steps, produced from a `List[TrajectoryPoint]` by
    `SystemStateMachine` right before sending). `parse_trajectory_progress()`
    also reuses this type for the INCOMING TRAJ_PROGRESS event, but
    there `x`/`y`/`angle` are raw steps, not cm/deg (see that
    function's docstring) — same context-dependent-units caveat as
    `Position` below.
    """
    t: float
    x: float
    y: float
    angle: float


@dataclass
class TrajectoryStepDelta:
    """
    A single WIRE-LEVEL trajectory point (2026-08-26, "Cambio 2026-08-26
    (trayectorias en pasos)", see docs/protocol.md) — what actually goes
    out as TRAJ_POINT, as opposed to TrajectoryPoint above (an absolute
    cm/deg/seconds point, used everywhere else in the app: CSV loading,
    trajectory generation, range validation, the live plot).

    Unlike TrajectoryPoint, this is a DELTA relative to the point
    before it (or, for the first delta, relative to wherever the
    platform physically is when the trajectory starts) — NOT an
    absolute position. `SystemStateMachine` is the only place that
    converts a `List[TrajectoryPoint]` into a `List[TrajectoryStepDelta]`
    before handing it to `ESP32Controller.send_trajectory()`.

    Attributes:
        dt_ms: Milliseconds elapsed since the previous point (integer,
            rounded — NOT necessarily uniform between points).
        dx_steps: Signed raw motor step delta on the X axis.
        dy_steps: Signed raw motor step delta on the Y axis.
        dangle_steps: Signed raw motor step delta on the angular axis.
    """
    dt_ms: int
    dx_steps: int
    dy_steps: int
    dangle_steps: int


@dataclass
class Position:
    """
    An absolute (x, y, angle) position, relative to the (0, 0, 0)
    established by the most recent HOME. Kept as a separate type from
    TrajectoryPoint because a position has no time component — used by
    GET_POSITION and the initial-position library
    (src/utils/position_library.py).

    UNITS ARE CONTEXT-DEPENDENT, unlike TrajectoryPoint (always cm/cm/
    degrees): `ESP32Controller.get_position()` (raw wire layer) returns
    this in raw motor STEP counts (see docs/protocol.md, Consulta de
    Posición); everywhere else in the app (SystemStateMachine.get_position(),
    position_library.py, UI code) it's cm/cm/degrees. Check which layer
    produced the instance before using its fields.
    """
    x: float
    y: float
    angle: float


@dataclass
class HomeLimits:
    """
    The 4 raw step values reported by a completed HOME
    ("READY:xmax:ymax:amin:amax" — see RESP_READY_PREFIX above). Y/X's
    MIN is not part of this: it is always 0 by definition (that axis's
    own limit switch IS raw step zero), so only their MAX travels.
    Angular's `angle_min`/`angle_max` are signed, already relative to
    horizontal = step 0. Units: raw motor steps, NOT cm/deg — converting
    is SystemStateMachine's job (see CalibrationSpace/
    _build_calibration_space in system_state.py), not this layer's.
    """
    x_max: float
    y_max: float
    angle_min: float
    angle_max: float


@dataclass
class ParsedResponse:
    """
    Result of parsing a single line received from the ESP32.

    Attributes:
        kind: One of "OK", "ERROR", "ACK", "READY", "PONG", "RUNNING",
              "FINISHED", "PAUSED", "ABORTED", "TRAJ_READY",
              "TRAJ_STORED", "TRAJ_PROGRESS", "POSITION", or "UNKNOWN"
              if the line did not match any known response format.
        payload: Additional data extracted from the line, when applicable
                 (e.g. the status string, the ACK index, the error
                 code/message, or READY's "xmax:ymax:amin:amax" — see
                 parse_home_limits()). None if not applicable.
        raw: The original line, unmodified, for logging/debugging.
    """
    kind: str
    payload: Optional[str]
    raw: str


# ---------------------------------------------------------------------------
# Message builders (Python -> wire format)
# ---------------------------------------------------------------------------

def build_ping() -> str:
    """Build a PING command frame."""
    return f"<{CMD_PING}>"


def build_home() -> str:
    """
    Build a HOME command frame (triggers homing + limit mapping).
    Response is "READY:xmax:ymax:amin:amax" — see RESP_READY_PREFIX and
    parse_home_limits().
    """
    return f"<{CMD_HOME}>"


def build_manual_move(axis: str, direction: str, steps: int) -> str:
    """
    Build a MANUAL movement command line.

    Args:
        axis: One of "X", "Y", "A" (A = sagittal angle axis).
        direction: "+" or "-" (sent on the wire as 1/0 respectively —
                   see _DIRECTION_WIRE_CODE).
        steps: Number of steps to move. Must be a positive integer;
               direction is what determines the sign of the movement.

    Raises:
        ValueError: If axis or direction are not among the allowed values,
                    or if steps is not positive. Validating here, at the
                    protocol boundary, prevents malformed commands from
                    ever reaching the serial link.
    """
    valid_axes = ("X", "Y", "A")
    if axis not in valid_axes:
        raise ValueError(f"Invalid axis '{axis}'. Must be one of {valid_axes}.")
    if direction not in ("+", "-"):
        raise ValueError(f"Invalid direction '{direction}'. Must be '+' or '-'.")
    if steps <= 0:
        raise ValueError(f"Steps must be a positive integer, got {steps}.")
    return f"<{CMD_MANUAL}:{axis}:{_DIRECTION_WIRE_CODE[direction]}:{steps}>"


def build_trajectory_begin(n_points: int) -> str:
    """
    Build a TRAJ_BEGIN command frame, announcing how many points will follow.

    Args:
        n_points: Number of TrajectoryPoint entries that will be sent
                  afterwards. Must match exactly what is sent, or the
                  ESP32 will respond with an error at TRAJ_END.
    """
    if n_points <= 0:
        raise ValueError(f"n_points must be positive, got {n_points}.")
    return f"<{CMD_TRAJ_BEGIN}:{n_points}>"


def build_trajectory_step_point(delta: TrajectoryStepDelta) -> str:
    """
    Build a TRAJ_POINT command frame for a single wire-level trajectory
    delta (see TrajectoryStepDelta and docs/protocol.md, "Cambio
    2026-08-26 (trayectorias en pasos)"). All 4 fields are plain
    integers (no decimals — dt_ms and every step count are already
    whole numbers by the time they reach here).
    """
    return (
        f"<{CMD_TRAJ_POINT}:"
        f"{delta.dt_ms}:{delta.dx_steps}:{delta.dy_steps}:{delta.dangle_steps}>"
    )


def build_trajectory_end() -> str:
    """Build a TRAJ_END command frame, marking the end of point transfer."""
    return f"<{CMD_TRAJ_END}>"


def build_run() -> str:
    """Build a RUN command frame (starts executing the stored trajectory)."""
    return f"<{CMD_RUN}>"


def build_pause() -> str:
    """Build a PAUSE command frame."""
    return f"<{CMD_PAUSE}>"


def build_resume() -> str:
    """Build a RESUME command frame."""
    return f"<{CMD_RESUME}>"


def build_abort() -> str:
    """
    Build an ABORT command frame: abandons a PAUSED trajectory entirely,
    returning to IDLE — unlike RESUME, does not continue execution.
    Only valid while PAUSED; the ESP32 enforces this.
    """
    return f"<{CMD_ABORT}>"


def build_get_position() -> str:
    """Build a GET_POSITION query frame."""
    return f"<{CMD_GET_POSITION}>"


# ---------------------------------------------------------------------------
# Response parser (wire format -> Python)
# ---------------------------------------------------------------------------

def parse_response(line: str) -> ParsedResponse:
    """
    Parse a single line received from the ESP32 into a ParsedResponse.

    This function is intentionally tolerant: it never raises on malformed
    input. An unrecognized line becomes kind="UNKNOWN" with the raw text
    preserved, so the caller (ESP32Controller) can decide how to log or
    react to unexpected input without the parser itself crashing the
    read loop.

    Args:
        line: A single line of text received over serial, WITHOUT the
              trailing newline (the caller/SerialManager is responsible
              for splitting incoming bytes into lines).

    Returns:
        A ParsedResponse describing what was received.
    """
    text = line.strip()

    if text == RESP_PONG:
        return ParsedResponse(kind="PONG", payload=None, raw=line)
    if text == RESP_OK:
        return ParsedResponse(kind="OK", payload=None, raw=line)
    if text == RESP_TRAJ_READY:
        return ParsedResponse(kind="TRAJ_READY", payload=None, raw=line)
    if text == RESP_TRAJ_STORED:
        return ParsedResponse(kind="TRAJ_STORED", payload=None, raw=line)
    if text == RESP_RUNNING:
        return ParsedResponse(kind="RUNNING", payload=None, raw=line)
    if text == RESP_FINISHED:
        return ParsedResponse(kind="FINISHED", payload=None, raw=line)
    if text == RESP_PAUSED:
        return ParsedResponse(kind="PAUSED", payload=None, raw=line)
    if text == RESP_ABORTED:
        return ParsedResponse(kind="ABORTED", payload=None, raw=line)

    if text.startswith(RESP_ACK_PREFIX):
        payload = text[len(RESP_ACK_PREFIX):]
        return ParsedResponse(kind="ACK", payload=payload, raw=line)

    if text.startswith(RESP_ERROR_PREFIX):
        # Keep the remainder ("<code>:<message>") intact as payload;
        # splitting code/message is the caller's choice, since some
        # callers may only care about the raw error text for logging.
        payload = text[len(RESP_ERROR_PREFIX):]
        return ParsedResponse(kind="ERROR", payload=payload, raw=line)

    if text.startswith(RESP_TRAJ_PROGRESS_PREFIX):
        payload = text[len(RESP_TRAJ_PROGRESS_PREFIX):]
        return ParsedResponse(kind="TRAJ_PROGRESS", payload=payload, raw=line)

    if text.startswith(RESP_POSITION_PREFIX):
        payload = text[len(RESP_POSITION_PREFIX):]
        return ParsedResponse(kind="POSITION", payload=payload, raw=line)

    if text.startswith(RESP_READY_PREFIX):
        payload = text[len(RESP_READY_PREFIX):]
        return ParsedResponse(kind="READY", payload=payload, raw=line)

    return ParsedResponse(kind="UNKNOWN", payload=None, raw=line)


def parse_trajectory_progress(payload: str) -> TrajectoryPoint:
    """
    Parse the payload of a TRAJ_PROGRESS response ("<t>:<x>:<y>:<angle>")
    into a TrajectoryPoint. NOTE: since 2026-08-26, `x`/`y`/`angle` are
    raw motor STEP counts, not cm/deg (see docs/protocol.md, Progreso
    de ejecución) — reusing TrajectoryPoint here is just a convenient
    4-number container, same caveat as Position (see its docstring);
    `t` is unaffected (still real seconds). Callers needing cm/deg must
    convert (see SystemStateMachine._on_progress).

    Args:
        payload: The ParsedResponse.payload of a "TRAJ_PROGRESS" response.

    Raises:
        ValueError: If the payload does not contain exactly 4 colon-
            separated numeric fields.
    """
    t, x, y, angle = payload.split(":")
    return TrajectoryPoint(t=float(t), x=float(x), y=float(y), angle=float(angle))


def parse_position(payload: str) -> Position:
    """
    Parse the payload of a POSITION response ("<x>:<y>:<angle>") into a
    Position. NOTE: since 2026-08-26 these fields are raw motor STEP
    counts, not cm/deg (see docs/protocol.md, Consulta de Posición) —
    reusing the Position dataclass here is just a convenient 3-float
    container, not a claim that the values are in real units. Callers
    needing cm/deg must convert (see SystemStateMachine.get_position()).

    Raises:
        ValueError: If the payload does not contain exactly 3 colon-
            separated numeric fields.
    """
    x, y, angle = payload.split(":")
    return Position(x=float(x), y=float(y), angle=float(angle))


def parse_home_limits(payload: str) -> HomeLimits:
    """
    Parse the payload of a READY response ("xmax:ymax:amin:amax") into
    a HomeLimits. All 4 fields are raw motor step counts (see
    HomeLimits' docstring) — Y/X's MIN is not part of the wire payload
    at all, since it's always 0 by definition.

    Raises:
        ValueError: If the payload does not contain exactly 4 colon-
            separated numeric fields.
    """
    x_max, y_max, angle_min, angle_max = payload.split(":")
    return HomeLimits(
        x_max=float(x_max),
        y_max=float(y_max),
        angle_min=float(angle_min),
        angle_max=float(angle_max),
    )


