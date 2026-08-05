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
CMD_STATUS = "STATUS"

CMD_MANUAL = "MANUAL"        # MANUAL:<axis>:<direction>:<steps>
CMD_STOP = "STOP"

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

CMD_GOTO = "GOTO"                   # GOTO:<x>:<y>:<angle>
CMD_GET_POSITION = "GET_POSITION"


# ---------------------------------------------------------------------------
# Response keywords (ESP32 -> RPi)
# ---------------------------------------------------------------------------

RESP_PONG = "PONG"
RESP_READY = "READY"
RESP_OK = "OK"
RESP_STOPPED = "STOPPED"
RESP_TRAJ_READY = "TRAJ_READY"
RESP_TRAJ_STORED = "TRAJ_STORED"
RESP_RUNNING = "RUNNING"
RESP_FINISHED = "FINISHED"
RESP_PAUSED = "PAUSED"
RESP_ABORTED = "ABORTED"

RESP_STATUS_PREFIX = "STATUS:"
RESP_ACK_PREFIX = "ACK:"
RESP_ERROR_PREFIX = "ERROR:"
RESP_TRAJ_PROGRESS_PREFIX = "TRAJ_PROGRESS:"
RESP_POSITION_PREFIX = "POSITION:"

# Calibration events (unsolicited, during HOMING) — see docs/protocol.md,
# "Calibration Events" section. On the wire these arrive wrapped in
# '<' '>' (e.g. "<LIMYMIN>", "<CAL_PROGRESS:Y:15.0000>") — the constants
# below are matched against the ALREADY-UNWRAPPED text (parse_response()
# strips the framing before any comparison happens). All 3 axes are
# treated uniformly on the wire: MIN carries no argument (that axis's
# min limit switch IS raw zero); MAX carries the value at that axis's
# max limit switch as its one argument; CAL_PROGRESS carries axis
# (string) then value (numeric), colon-separated. This is the one
# ESP32 -> RPi exception to the "responses are unframed" rule (see
# Transport in docs/protocol.md).
#
# The angular axis's "relative to horizontal" reinterpretation (its raw
# limit-relative sweep is NOT the same as degrees from level) is a pure
# RPi-side business-logic concern, NOT a wire-format one — see
# SystemStateMachine.ANGLE_HORIZONTAL_OFFSET_DEG in system_state.py.
# protocol.py stays hardware/business-logic-agnostic (see module
# docstring) and reports the raw, limit-relative values for all 3 axes.
RESP_LIM_Y_MIN = "LIMYMIN"
RESP_LIM_X_MIN = "LIMXMIN"
RESP_LIM_ANG_MIN = "LIMANGMIN"

RESP_LIM_Y_MAX_PREFIX = "LIMYMAX:"
RESP_LIM_X_MAX_PREFIX = "LIMXMAX:"
RESP_LIM_ANG_MAX_PREFIX = "LIMANGMAX:"

RESP_CAL_PROGRESS_PREFIX = "CAL_PROGRESS:"

# Maps a ParsedResponse.kind to (axis, bound) for every calibration
# limit event, so callers don't need to know the wire tokens themselves
# (esp32_controller.py's dispatch uses this).
CALIBRATION_LIMIT_KINDS = {
    "LIMYMIN": ("Y", "MIN"),
    "LIMXMIN": ("X", "MIN"),
    "LIMANGMIN": ("A", "MIN"),
    "LIMYMAX": ("Y", "MAX"),
    "LIMXMAX": ("X", "MAX"),
    "LIMANGMAX": ("A", "MAX"),
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class TrajectoryPoint:
    """
    A single sample of a gait trajectory.

    Attributes:
        t: Time value, in seconds, relative to the start of the cycle.
        x: Horizontal axis position (units defined by the mechanical
           design / firmware calibration, e.g. mm).
        y: Vertical axis position (same unit convention as x).
        angle: Sagittal rotation angle, in degrees.
    """
    t: float
    x: float
    y: float
    angle: float


@dataclass
class Position:
    """
    An absolute (x, y, angle) position, in the same units and reference
    frame as TrajectoryPoint's x/y/angle (cm, cm, degrees, relative to
    the (0, 0, 0) established by the most recent HOME). Kept as a
    separate type from TrajectoryPoint because a position has no time
    component — used by GOTO/GET_POSITION and the initial-position
    library (src/utils/position_library.py).
    """
    x: float
    y: float
    angle: float


@dataclass
class ParsedResponse:
    """
    Result of parsing a single line received from the ESP32.

    Attributes:
        kind: One of "OK", "ERROR", "STATUS", "ACK", "READY", "PONG",
              "RUNNING", "FINISHED", "PAUSED", "ABORTED", "STOPPED",
              "TRAJ_READY", "TRAJ_STORED", "TRAJ_PROGRESS", "POSITION",
              "LIMYMIN", "LIMXMIN", "LIMANGMIN", "LIMYMAX", "LIMXMAX",
              "LIMANGMAX", "CAL_PROGRESS" (see CALIBRATION_LIMIT_KINDS),
              or "UNKNOWN" if the line did not match any known response
              format.
        payload: Additional data extracted from the line, when applicable
                 (e.g. the status string, the ACK index, or the error
                 code/message). None if not applicable.
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
    """Build a HOME command frame (triggers homing + limit mapping)."""
    return f"<{CMD_HOME}>"


def build_status_query() -> str:
    """Build a STATUS command frame."""
    return f"<{CMD_STATUS}>"


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


def build_stop() -> str:
    """Build a STOP command frame (emergency stop / pause during run)."""
    return f"<{CMD_STOP}>"


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


def build_trajectory_point(point: TrajectoryPoint) -> str:
    """
    Build a TRAJ_POINT command frame for a single trajectory sample.

    Numeric values are formatted with fixed precision (4 decimals) to
    keep message size predictable and avoid locale-dependent formatting
    surprises (e.g. comma vs. dot as decimal separator).
    """
    return (
        f"<{CMD_TRAJ_POINT}:"
        f"{point.t:.4f}:{point.x:.4f}:{point.y:.4f}:{point.angle:.4f}>"
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


def build_goto_position(position: Position) -> str:
    """
    Build a GOTO command frame, requesting an absolute move to the given
    (x, y, angle) position. Only valid while the system is IDLE, same
    restriction as MANUAL — the ESP32 enforces this.
    """
    return (
        f"<{CMD_GOTO}:"
        f"{position.x:.4f}:{position.y:.4f}:{position.angle:.4f}>"
    )


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

    # Calibration events (LIM*MIN/MAX, CAL_PROGRESS) are, by explicit
    # request, the only ESP32 -> RPi messages framed with '<' '>' (see
    # docs/protocol.md, Calibration Events "Framing exception") — every
    # other response matched below is unframed, so this strip is a
    # no-op for all of them (none start/end with '<'/'>').
    if text.startswith("<") and text.endswith(">"):
        text = text[1:-1]

    if text == RESP_PONG:
        return ParsedResponse(kind="PONG", payload=None, raw=line)
    if text == RESP_READY:
        return ParsedResponse(kind="READY", payload=None, raw=line)
    if text == RESP_OK:
        return ParsedResponse(kind="OK", payload=None, raw=line)
    if text == RESP_STOPPED:
        return ParsedResponse(kind="STOPPED", payload=None, raw=line)
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

    if text.startswith(RESP_STATUS_PREFIX):
        payload = text[len(RESP_STATUS_PREFIX):]
        return ParsedResponse(kind="STATUS", payload=payload, raw=line)

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

    if text == RESP_LIM_Y_MIN:
        return ParsedResponse(kind="LIMYMIN", payload=None, raw=line)
    if text == RESP_LIM_X_MIN:
        return ParsedResponse(kind="LIMXMIN", payload=None, raw=line)
    if text == RESP_LIM_ANG_MIN:
        return ParsedResponse(kind="LIMANGMIN", payload=None, raw=line)

    if text.startswith(RESP_LIM_Y_MAX_PREFIX):
        payload = text[len(RESP_LIM_Y_MAX_PREFIX):]
        return ParsedResponse(kind="LIMYMAX", payload=payload, raw=line)
    if text.startswith(RESP_LIM_X_MAX_PREFIX):
        payload = text[len(RESP_LIM_X_MAX_PREFIX):]
        return ParsedResponse(kind="LIMXMAX", payload=payload, raw=line)
    if text.startswith(RESP_LIM_ANG_MAX_PREFIX):
        payload = text[len(RESP_LIM_ANG_MAX_PREFIX):]
        return ParsedResponse(kind="LIMANGMAX", payload=payload, raw=line)

    if text.startswith(RESP_CAL_PROGRESS_PREFIX):
        payload = text[len(RESP_CAL_PROGRESS_PREFIX):]
        return ParsedResponse(kind="CAL_PROGRESS", payload=payload, raw=line)

    return ParsedResponse(kind="UNKNOWN", payload=None, raw=line)


def parse_trajectory_progress(payload: str) -> TrajectoryPoint:
    """
    Parse the payload of a TRAJ_PROGRESS response ("<t>:<x>:<y>:<angle>")
    into a TrajectoryPoint.

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
    Position.

    Raises:
        ValueError: If the payload does not contain exactly 3 colon-
            separated numeric fields.
    """
    x, y, angle = payload.split(":")
    return Position(x=float(x), y=float(y), angle=float(angle))


def parse_calibration_progress(payload: str):
    """
    Parse the payload of a CAL_PROGRESS response ("axis:value", colon-
    separated — see docs/protocol.md, Calibration Events "Framing
    exception") into an (axis, value) tuple. `axis` is one of "Y", "X",
    "A"; `value` is the distance covered so far from that axis's min,
    in real units.

    Raises:
        ValueError: If the payload does not contain exactly 2
            colon-separated fields, or value is not numeric.
    """
    axis, value = payload.split(":")
    return axis, float(value)
