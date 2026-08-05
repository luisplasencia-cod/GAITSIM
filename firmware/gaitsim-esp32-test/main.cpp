/*
 * gaitsim-esp32-test — Minimal protocol-compliant firmware for testing.
 *
 * This firmware implements the serial protocol defined in
 * docs/protocol.md WITHOUT controlling real motors. It simulates
 * timing (homing, trajectory execution) so the Raspberry Pi software
 * (SerialManager, ESP32Controller) can be developed and tested
 * end-to-end before the definitive firmware is available.
 *
 * Baud rate: 115200 (per protocol spec).
 */

#include <Arduino.h>

// ---------------------------------------------------------------------
// System state
// ---------------------------------------------------------------------

enum SystemState {
  STATE_DISCONNECTED,  // before HOME has ever succeeded
  STATE_IDLE,           // homed, ready, not running
  STATE_HOMING,
  STATE_RECEIVING_TRAJECTORY,
  STATE_RUNNING,
  STATE_PAUSED
};

SystemState currentState = STATE_DISCONNECTED;

// ---------------------------------------------------------------------
// Tracked position (x, y, angle), reset to (0, 0, 0) by HOME
// ---------------------------------------------------------------------
// The Raspberry Pi never converts steps to real units itself (see
// docs/protocol.md, Absolute Positioning Commands) — it relies on this
// firmware to track the result of MANUAL moves and report it back via
// GET_POSITION. TEST_UNITS_PER_STEP is an arbitrary placeholder
// (cm or deg per step) purely for exercising that flow with the test
// firmware; it does not represent any real mechanical calibration.
float posX = 0.0;
float posY = 0.0;
float posAngle = 0.0;
const float TEST_UNITS_PER_STEP = 0.05;

// ---------------------------------------------------------------------
// Trajectory buffer (simulated storage)
// ---------------------------------------------------------------------
// Fixed upper bound for this test firmware; the definitive firmware
// may size this differently based on real available RAM.
const int MAX_TRAJECTORY_POINTS = 300;

struct TrajectoryPoint {
  float t;
  float x;
  float y;
  float angle;
};

TrajectoryPoint trajectoryBuffer[MAX_TRAJECTORY_POINTS];
int expectedPointCount = 0;
int receivedPointCount = 0;
bool trajectoryStored = false;

// ---------------------------------------------------------------------
// Serial line buffer
// ---------------------------------------------------------------------
// RPi -> ESP32 commands are framed as "<...>" (no trailing '\n'), per
// docs/protocol.md — receivingCommand tracks whether we're currently
// inside a frame (i.e. already saw '<', still waiting for '>'). Bytes
// seen outside a frame are discarded. ESP32 -> RPi responses are
// unaffected by this and still go out as plain '\n'-terminated text
// (see sendResponse()/sendError()).
bool receivingCommand = false;
String inputLine = "";

// pollSerial() is defined near the bottom of this file (it calls
// processLine(), defined in the dispatch section below), but handleRun()
// needs to call it WHILE a trajectory is executing, so PAUSE/STOP/RESUME
// commands actually get read instead of sitting unprocessed in the UART
// buffer until the whole run finishes.
void pollSerial();

// ---------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------

void sendResponse(const String &msg) {
  Serial.println(msg);
}

void sendError(const String &code, const String &message) {
  sendResponse("ERROR:" + code + ":" + message);
}

// Splits a command line by ':' into up to maxParts pieces.
// Returns the number of parts found.
int splitCommand(const String &line, String parts[], int maxParts) {
  int count = 0;
  int start = 0;
  int idx;
  while (count < maxParts) {
    idx = line.indexOf(':', start);
    if (idx == -1) {
      parts[count++] = line.substring(start);
      break;
    }
    parts[count++] = line.substring(start, idx);
    start = idx + 1;
  }
  return count;
}

// ---------------------------------------------------------------------
// Command handlers
// ---------------------------------------------------------------------

void handlePing() {
  sendResponse("PONG");
}

// ---------------------------------------------------------------------
// Calibration (limit-mapping) simulation
// ---------------------------------------------------------------------
// Placeholder full-travel values per axis (cm for Y/X, degrees for A) —
// arbitrary, purely to exercise the LIM*MIN -> CAL_PROGRESS -> LIM*MAX
// event sequence defined in docs/protocol.md; not tied to any real
// mechanical dimension (same spirit as TEST_UNITS_PER_STEP above). All
// 3 axes are raw/limit-relative here: LIM*MIN is that axis's zero, and
// CAL_PROGRESS/LIM*MAX count up from it. The angular axis's "relative
// to horizontal" reinterpretation (there's a real physical offset
// between "touching the lower limit switch" and "level") is applied
// entirely on the Raspberry Pi side (see
// SystemStateMachine.ANGLE_HORIZONTAL_OFFSET_DEG in system_state.py) —
// this firmware doesn't need to know about it.
const float CAL_Y_MAX_CM = 100.0;
const float CAL_X_MAX_CM = 100.0;
const float CAL_ANGLE_MAX_DEG = 120.0;

// Simulated duration of the travel-to-max phase, per axis, and how
// often CAL_PROGRESS is sent during it (so the RPi side has something
// to plot progressively, not all-at-once).
const unsigned long CAL_AXIS_DURATION_MS = 4000;
const unsigned long CAL_PROGRESS_INTERVAL_MS = 200;

// Runs one axis's MIN -> travel -> MAX sequence.
//   limLabel: the LIM{...} token infix, e.g. "Y", "X", "ANG"
//   axisCode: the axis letter used in CAL_PROGRESS payloads (matches
//             MANUAL/GOTO's axis convention: X, Y, A)
//   maxValue: full simulated travel for this axis (cm or deg)
//
// Unlike every other outgoing response in this file, these calibration
// events are wrapped in '<' '>' and CAL_PROGRESS's two arguments are
// colon-separated (string axis first, numeric value second) — the same
// framing/argument convention normally reserved for RPi -> ESP32
// commands, applied here by explicit request (see docs/protocol.md,
// Calibration Events "Framing exception"). Every other sendResponse()
// call in this file stays unframed.
void simulateAxisCalibration(const String &limLabel, const String &axisCode,
                              float maxValue) {
  sendResponse("<LIM" + limLabel + "MIN>");

  unsigned long start = millis();
  unsigned long elapsed = 0;
  while (elapsed < CAL_AXIS_DURATION_MS) {
    delay(CAL_PROGRESS_INTERVAL_MS);
    elapsed = millis() - start;
    if (elapsed > CAL_AXIS_DURATION_MS) elapsed = CAL_AXIS_DURATION_MS;
    float value = maxValue * (float(elapsed) / float(CAL_AXIS_DURATION_MS));
    sendResponse("<CAL_PROGRESS:" + axisCode + ":" + String(value, 4) + ">");
  }

  sendResponse("<LIM" + limLabel + "MAX:" + String(maxValue, 4) + ">");
}

void handleHome() {
  currentState = STATE_HOMING;

  // Full limit-mapping sweep, one axis at a time, in the order defined
  // by docs/protocol.md's Calibration Events section (Y, then X, then
  // Angular).
  simulateAxisCalibration("Y", "Y", CAL_Y_MAX_CM);
  simulateAxisCalibration("X", "X", CAL_X_MAX_CM);
  simulateAxisCalibration("ANG", "A", CAL_ANGLE_MAX_DEG);

  posX = 0.0;
  posY = 0.0;
  posAngle = 0.0;
  currentState = STATE_IDLE;
  sendResponse("READY");
}

void handleStatus() {
  String stateStr;
  switch (currentState) {
    case STATE_DISCONNECTED: stateStr = "DISCONNECTED"; break;
    case STATE_IDLE: stateStr = "IDLE"; break;
    case STATE_HOMING: stateStr = "HOMING"; break;
    case STATE_RECEIVING_TRAJECTORY: stateStr = "RECEIVING_TRAJECTORY"; break;
    case STATE_RUNNING: stateStr = "RUNNING"; break;
    case STATE_PAUSED: stateStr = "PAUSED"; break;
  }
  sendResponse("STATUS:" + stateStr);
}

void handleManual(const String &line) {
  if (currentState != STATE_IDLE) {
    sendError("INVALID_STATE", "manual move not allowed in current state");
    return;
  }
  // Format: MANUAL:<axis>:<direction>:<steps> — direction is an integer
  // on the wire (1 = "+", 0 = "-"), per docs/protocol.md: any command
  // with 2+ arguments must send its first argument as string (axis) and
  // every argument after that as an integer.
  String parts[4];
  int n = splitCommand(line, parts, 4);
  if (n != 4) {
    sendError("MALFORMED", "expected MANUAL:<axis>:<direction>:<steps>");
    return;
  }
  String axis = parts[1];
  int direction = parts[2].toInt();
  int steps = parts[3].toInt();

  if (axis != "X" && axis != "Y" && axis != "A") {
    sendError("INVALID_AXIS", axis);
    return;
  }
  if (direction != 1 && direction != 0) {
    sendError("INVALID_DIRECTION", String(direction));
    return;
  }
  // Simulated: no real limit checking yet.
  float delta = steps * TEST_UNITS_PER_STEP * (direction == 1 ? 1.0 : -1.0);
  if (axis == "X") posX += delta;
  else if (axis == "Y") posY += delta;
  else if (axis == "A") posAngle += delta;

  sendResponse("OK");
}

void handleGoTo(const String &line) {
  if (currentState != STATE_IDLE) {
    sendError("INVALID_STATE", "goto not allowed in current state");
    return;
  }
  // Format: GOTO:<x>,<y>,<angle>
  int colonIdx = line.indexOf(':');
  String data = line.substring(colonIdx + 1);

  int c1 = data.indexOf(',');
  int c2 = data.indexOf(',', c1 + 1);
  if (c1 == -1 || c2 == -1) {
    sendError("MALFORMED", "expected GOTO:<x>,<y>,<angle>");
    return;
  }

  posX = data.substring(0, c1).toFloat();
  posY = data.substring(c1 + 1, c2).toFloat();
  posAngle = data.substring(c2 + 1).toFloat();

  // Simulated: instantaneous move, no real motor timing yet.
  sendResponse("OK");
}

void handleGetPosition() {
  sendResponse("POSITION:" + String(posX, 4) + "," + String(posY, 4) +
               "," + String(posAngle, 4));
}

void handleStop() {
  if (currentState == STATE_RUNNING) {
    currentState = STATE_PAUSED;
  }
  sendResponse("STOPPED");
}

void handleTrajBegin(const String &line) {
  if (currentState != STATE_IDLE) {
    sendError("INVALID_STATE", "cannot begin trajectory in current state");
    return;
  }
  String parts[2];
  int n = splitCommand(line, parts, 2);
  if (n != 2) {
    sendError("MALFORMED", "expected TRAJ_BEGIN:<n_points>");
    return;
  }
  int n_points = parts[1].toInt();
  if (n_points <= 0 || n_points > MAX_TRAJECTORY_POINTS) {
    sendError("INVALID_POINT_COUNT", String(n_points));
    return;
  }
  expectedPointCount = n_points;
  receivedPointCount = 0;
  trajectoryStored = false;
  currentState = STATE_RECEIVING_TRAJECTORY;
  sendResponse("TRAJ_READY");
}

void handleTrajPoint(const String &line) {
  if (currentState != STATE_RECEIVING_TRAJECTORY) {
    sendError("INVALID_STATE", "not currently receiving a trajectory");
    return;
  }
  if (receivedPointCount >= expectedPointCount) {
    sendError("POINT_COUNT_MISMATCH", "received more points than announced");
    return;
  }
  // Format: TRAJ_POINT:<t>,<x>,<y>,<angle>
  int colonIdx = line.indexOf(':');
  String data = line.substring(colonIdx + 1);

  int c1 = data.indexOf(',');
  int c2 = data.indexOf(',', c1 + 1);
  int c3 = data.indexOf(',', c2 + 1);
  if (c1 == -1 || c2 == -1 || c3 == -1) {
    sendError("MALFORMED", "expected TRAJ_POINT:<t>,<x>,<y>,<angle>");
    return;
  }

  TrajectoryPoint pt;
  pt.t = data.substring(0, c1).toFloat();
  pt.x = data.substring(c1 + 1, c2).toFloat();
  pt.y = data.substring(c2 + 1, c3).toFloat();
  pt.angle = data.substring(c3 + 1).toFloat();

  trajectoryBuffer[receivedPointCount] = pt;
  receivedPointCount++;

  sendResponse("ACK:" + String(receivedPointCount - 1));
}

void handleTrajEnd() {
  if (currentState != STATE_RECEIVING_TRAJECTORY) {
    sendError("INVALID_STATE", "not currently receiving a trajectory");
    return;
  }
  if (receivedPointCount != expectedPointCount) {
    sendError("POINT_COUNT_MISMATCH",
               "expected " + String(expectedPointCount) +
               ", got " + String(receivedPointCount));
    currentState = STATE_IDLE;
    return;
  }
  trajectoryStored = true;
  currentState = STATE_IDLE;
  sendResponse("TRAJ_STORED");
}

void handleRun() {
  if (currentState != STATE_IDLE || !trajectoryStored) {
    sendError("INVALID_STATE", "no trajectory stored or system not idle");
    return;
  }
  currentState = STATE_RUNNING;
  sendResponse("RUNNING");

  // Simulate execution time proportional to point count (placeholder).
  // TEST-ONLY: emits one TRAJ_PROGRESS per point at a fixed 120ms cadence
  // so the RPi side can live-plot something visible. This fixed interval
  // ignores each point's real `t` value (still sent as data, for the
  // plot's X axis) and does NOT represent real execution timing — the
  // definitive firmware will have its own real per-point timing.
  //
  // IMPORTANT: the only place this sketch normally reads Serial is
  // loop()'s top-level while-loop. Since this whole run happens nested
  // inside a single call to handleRun(), a PAUSE/STOP sent mid-run would
  // otherwise sit unread in the UART buffer until every point finished.
  // pollSerial() is called explicitly below (both while waiting between
  // points and while paused) so those commands are actually processed
  // in time, and RESUME can continue the same for loop where it left off.
  for (int i = 0; i < receivedPointCount; i++) {
    // Block here, without unwinding the loop, for as long as we're
    // paused/stopped. handlePause()/handleStop() set STATE_PAUSED;
    // handleResume() (reached via pollSerial() -> processLine() below)
    // sets it back to STATE_RUNNING, which ends this wait in place.
    while (currentState == STATE_PAUSED) {
      pollSerial();
      delay(5);
    }
    if (currentState != STATE_RUNNING) {
      // Disconnected or some other unexpected transition; abort the run.
      return;
    }

    TrajectoryPoint &pt = trajectoryBuffer[i];
    // Keep the tracked position (posX/posY/posAngle, reported by
    // GET_POSITION) in step with actual execution, not just with
    // MANUAL/GOTO — otherwise GET_POSITION after a PAUSE/ABORT would
    // report stale pre-run coordinates instead of where the system
    // actually stopped, which safe_return_to_position() (system_state.py)
    // depends on. TRAJ_POINT coordinates are already absolute/HOME-
    // origin-relative (trajectory_screen.py offsets them before send),
    // so no further conversion is needed here.
    posX = pt.x;
    posY = pt.y;
    posAngle = pt.angle;
    sendResponse("TRAJ_PROGRESS:" + String(pt.t, 4) + "," + String(pt.x, 4) +
                 "," + String(pt.y, 4) + "," + String(pt.angle, 4));

    // Wait ~120ms before the next point (TEST-ONLY cadence), polling
    // throughout so a PAUSE/STOP arriving mid-wait is noticed promptly
    // instead of only after the full run finishes.
    unsigned long waitStart = millis();
    while (millis() - waitStart < 120) {
      pollSerial();
      if (currentState != STATE_RUNNING) {
        break; // paused/stopped mid-wait; the loop-top wait will block on it
      }
      delay(5);
    }
  }

  currentState = STATE_IDLE;
  sendResponse("FINISHED");
}

void handlePause() {
  if (currentState == STATE_RUNNING) {
    currentState = STATE_PAUSED;
    sendResponse("PAUSED");
  } else {
    sendError("INVALID_STATE", "not currently running");
  }
}

void handleResume() {
  if (currentState == STATE_PAUSED) {
    currentState = STATE_RUNNING;
    sendResponse("RUNNING");
  } else {
    sendError("INVALID_STATE", "not currently paused");
  }
}

void handleAbort() {
  // Abandons a paused trajectory entirely (vs. RESUME, which continues
  // it) — the operator observed a fault and wants to restart the trial
  // or run a different one. Setting currentState here is enough:
  // handleRun()'s wait loop (`while (currentState == STATE_PAUSED)`)
  // exits as soon as this runs (via the same nested-pollSerial() path
  // already used for PAUSE/RESUME mid-run), sees currentState is no
  // longer STATE_RUNNING either, and returns without sending FINISHED —
  // no separate abort flag needed.
  if (currentState == STATE_PAUSED) {
    currentState = STATE_IDLE;
    sendResponse("ABORTED");
  } else {
    sendError("INVALID_STATE", "not currently paused");
  }
}

// ---------------------------------------------------------------------
// Command dispatch
// ---------------------------------------------------------------------

void processLine(const String &line) {
  if (line.startsWith("PING")) handlePing();
  else if (line.startsWith("HOME")) handleHome();
  else if (line.startsWith("STATUS")) handleStatus();
  else if (line.startsWith("MANUAL")) handleManual(line);
  else if (line.startsWith("STOP")) handleStop();
  else if (line.startsWith("TRAJ_BEGIN")) handleTrajBegin(line);
  else if (line.startsWith("TRAJ_POINT")) handleTrajPoint(line);
  else if (line.startsWith("TRAJ_END")) handleTrajEnd();
  else if (line.startsWith("RUN")) handleRun();
  else if (line.startsWith("PAUSE")) handlePause();
  else if (line.startsWith("RESUME")) handleResume();
  else if (line.startsWith("ABORT")) handleAbort();
  else if (line.startsWith("GOTO")) handleGoTo(line);
  else if (line.startsWith("GET_POSITION")) handleGetPosition();
  else sendError("UNKNOWN_COMMAND", line);
}

// ---------------------------------------------------------------------
// Arduino entry points
// ---------------------------------------------------------------------

void setup() {
  Serial.begin(115200);
  currentState = STATE_DISCONNECTED;
}

void pollSerial() {
  while (Serial.available() > 0) {

    char c = Serial.read();

    if (!receivingCommand) {
      // Idle between frames: discard anything that isn't the start of
      // one. Once '<' arrives, start accumulating the command content.
      if (c == '<') {
        receivingCommand = true;
        inputLine = "";
      }
      continue;
    }

    if (c == '>') {
      receivingCommand = false;

      if (inputLine.length() > 0) {
        // Clear the shared global BEFORE dispatching, not after: a
        // handler like handleRun() can itself call pollSerial() again
        // (nested, while a trajectory is executing) to notice PAUSE/
        // STOP/RESUME. If inputLine were cleared only after processLine()
        // returns, that nested call would keep appending onto the SAME
        // still-populated buffer (e.g. "RUN" + "PAUSE" -> "RUNPAUSE",
        // which startsWith("RUN") and wrongly re-triggers handleRun()).
        String line = inputLine;
        inputLine = "";
        processLine(line);
      }
    }
    else {
      inputLine += c;
    }
  }
}

void loop() {
  pollSerial();
}
