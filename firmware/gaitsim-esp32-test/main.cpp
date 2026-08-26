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
// Tracked NATIVELY in raw motor steps (2026-08-26) — matches how
// GET_POSITION reports it AND how TRAJ_POINT now commands movement
// (see docs/protocol.md, "Cambio 2026-08-26 (trayectorias en pasos)").
// No unit conversion happens anywhere in this firmware anymore: the
// Raspberry Pi does all cm/deg<->steps conversion on its side before
// ever putting a number on the wire.
long posXSteps = 0;
long posYSteps = 0;
long posAngleSteps = 0;

// ---------------------------------------------------------------------
// Trajectory buffer (simulated storage)
// ---------------------------------------------------------------------
// Fixed upper bound for this test firmware; the definitive firmware
// may size this differently based on real available RAM.
const int MAX_TRAJECTORY_POINTS = 300;

// Each stored point is a DELTA as received on the wire (dtMs since the
// previous point, signed step deltas per axis) — see handleTrajPoint().
// ALREADY ACCUMULATED into absolute step positions at receive time
// (xSteps/ySteps/angleSteps below are cumulative, not the raw delta),
// so handleRun() can just apply them directly without re-deriving
// anything at execution time.
struct TrajectoryStepPoint {
  unsigned long dtMs;
  long xSteps;
  long ySteps;
  long angleSteps;
};

TrajectoryStepPoint trajectoryBuffer[MAX_TRAJECTORY_POINTS];
int expectedPointCount = 0;
int receivedPointCount = 0;
bool trajectoryStored = false;
// Running accumulator while RECEIVING_TRAJECTORY — starts at whatever
// posXSteps/posYSteps/posAngleSteps were when TRAJ_BEGIN arrived (see
// handleTrajBegin()), since every delta is relative to the point
// before it, and the first delta is relative to wherever the platform
// currently is.
long trajAccumXSteps = 0;
long trajAccumYSteps = 0;
long trajAccumAngleSteps = 0;

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
// needs to call it WHILE a trajectory is executing, so PAUSE/RESUME
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
// Real max step-count ceilings for Y/X (2026-08-26), from the
// teammate's definitive-firmware motion-data generation script
// (leadscrew pitch + motor microstepping of the real rig) — no longer
// arbitrary placeholders like TEST_UNITS_PER_STEP above, since
// LIM{AXIS}MAX now reports raw motor steps on the wire instead of the
// ESP32 pre-converting to cm/deg (see docs/protocol.md, Calibration
// Events, "Cambio 2026-08-26"). Y/X are raw/limit-relative: LIM*MIN is
// that axis's zero (step 0), and LIM*MAX counts up from it.
const long CAL_Y_MAX_STEPS = 72000;
const long CAL_X_MAX_STEPS = 48000;

// Angular axis limits (2026-08-26, "Cambio 2026-08-26 (eje angular)"):
// UNLIKE Y/X, this axis's physical limit switches don't sit at
// level/horizontal, so the ESP32 itself reports LIMANGMIN/LIMANGMAX as
// SIGNED step counts already relative to horizontal = step 0, instead
// of the Raspberry Pi applying a fixed offset afterward. These match
// the teammate's HOMING_ZERO_MIN_K_STEPS/HOMING_ZERO_MAX_K_STEPS
// (angular gearbox ratio + motor microstepping of the real rig).
const long CAL_ANG_MIN_STEPS = -5000;
const long CAL_ANG_MAX_STEPS = 5400;

// Simulated duration of the travel-to-max phase, per axis — no
// intermediate progress is reported anymore (CAL_PROGRESS was removed
// from the protocol by explicit request), this is purely so HOMING
// takes a visible moment instead of resolving instantly.
const unsigned long CAL_AXIS_DURATION_MS = 4000;

// Runs one axis's MIN -> travel -> MAX sequence for Y/X, where MIN is
// always step 0 (no argument on the wire).
//   limLabel: the LIM{...} token infix, e.g. "Y", "X"
//   maxSteps: full simulated travel for this axis, in raw motor steps
//
// Unlike every other outgoing response in this file, these calibration
// events are wrapped in '<' '>'.
void simulateAxisCalibration(const String &limLabel, long maxSteps) {
  sendResponse("<LIM" + limLabel + "MIN>");
  delay(CAL_AXIS_DURATION_MS);
  sendResponse("<LIM" + limLabel + "MAX:" + String(maxSteps) + ">");
}

// Runs the angular axis's MIN -> travel -> MAX sequence. UNLIKE
// simulateAxisCalibration() above, both MIN and MAX carry a signed
// step value here, since this axis's zero is horizontal/level, not
// "touching the lower limit switch" (see CAL_ANG_MIN_STEPS/
// CAL_ANG_MAX_STEPS above).
void simulateAngularCalibration(long minSteps, long maxSteps) {
  sendResponse("<LIMANGMIN:" + String(minSteps) + ">");
  delay(CAL_AXIS_DURATION_MS);
  sendResponse("<LIMANGMAX:" + String(maxSteps) + ">");
}

void handleHome() {
  currentState = STATE_HOMING;

  // Full limit-mapping sweep, one axis at a time, in the order defined
  // by docs/protocol.md's Calibration Events section (Y, then X, then
  // Angular).
  simulateAxisCalibration("Y", CAL_Y_MAX_STEPS);
  simulateAxisCalibration("X", CAL_X_MAX_STEPS);
  simulateAngularCalibration(CAL_ANG_MIN_STEPS, CAL_ANG_MAX_STEPS);

  posXSteps = 0;
  posYSteps = 0;
  posAngleSteps = 0;
  currentState = STATE_IDLE;
  sendResponse("READY");
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
  // Simulated: no real limit checking yet. Position is step-native, so
  // this is a direct add — no unit conversion needed (unlike before
  // 2026-08-26, when position was tracked in cm/deg internally).
  long delta = (long)steps * (direction == 1 ? 1 : -1);
  if (axis == "X") posXSteps += delta;
  else if (axis == "Y") posYSteps += delta;
  else if (axis == "A") posAngleSteps += delta;

  sendResponse("OK");
}

void handleGetPosition() {
  sendResponse("POSITION:" + String(posXSteps) + ":" + String(posYSteps) +
               ":" + String(posAngleSteps));
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
  // Every delta point is relative to the one before it, and the FIRST
  // delta is relative to wherever the platform currently is — so the
  // accumulator starts at the tracked position, not at zero.
  trajAccumXSteps = posXSteps;
  trajAccumYSteps = posYSteps;
  trajAccumAngleSteps = posAngleSteps;
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
  // Format (2026-08-26): TRAJ_POINT:<dt_ms>:<dx_steps>:<dy_steps>:<dangle_steps>
  // — a signed step DELTA from the previous point (or from the current
  // tracked position, for the first point), NOT an absolute position
  // (see docs/protocol.md, "Cambio 2026-08-26 (trayectorias en pasos)").
  String parts[5];
  int n = splitCommand(line, parts, 5);
  if (n != 5) {
    sendError("MALFORMED", "expected TRAJ_POINT:<dt_ms>:<dx>:<dy>:<dangle>");
    return;
  }
  unsigned long dtMs = (unsigned long)parts[1].toInt();
  long dxSteps = parts[2].toInt();
  long dySteps = parts[3].toInt();
  long dangleSteps = parts[4].toInt();

  trajAccumXSteps += dxSteps;
  trajAccumYSteps += dySteps;
  trajAccumAngleSteps += dangleSteps;

  trajectoryBuffer[receivedPointCount] = {dtMs, trajAccumXSteps, trajAccumYSteps, trajAccumAngleSteps};
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
  // inside a single call to handleRun(), a PAUSE sent mid-run would
  // otherwise sit unread in the UART buffer until every point finished.
  // pollSerial() is called explicitly below (both while waiting between
  // points and while paused) so those commands are actually processed
  // in time, and RESUME can continue the same for loop where it left off.
  //
  // elapsedMs accumulates each point's dtMs into a running total, purely
  // to give TRAJ_PROGRESS a `t` value for the live plot's X axis (see
  // below) — unrelated to the fixed 120ms TEST-ONLY wait between
  // sendResponse() calls a few lines down, which is what actually
  // paces this loop, same as before 2026-08-26.
  unsigned long elapsedMs = 0;
  for (int i = 0; i < receivedPointCount; i++) {
    // Block here, without unwinding the loop, for as long as we're
    // paused. handlePause() sets STATE_PAUSED; handleResume() (reached
    // via pollSerial() -> processLine() below) sets it back to
    // STATE_RUNNING, which ends this wait in place.
    while (currentState == STATE_PAUSED) {
      pollSerial();
      delay(5);
    }
    if (currentState != STATE_RUNNING) {
      // Disconnected or some other unexpected transition; abort the run.
      return;
    }

    TrajectoryStepPoint &pt = trajectoryBuffer[i];
    // Keep the tracked position (posXSteps/posYSteps/posAngleSteps,
    // reported by GET_POSITION) in step with actual execution, not
    // just with MANUAL — otherwise GET_POSITION after a PAUSE/ABORT
    // would report stale pre-run coordinates instead of where the
    // system actually stopped, which safe_return_to_position()
    // (system_state.py) depends on. pt already holds the ABSOLUTE
    // accumulated step position (accumulated once, at receive time in
    // handleTrajPoint()), so this is a direct assignment.
    posXSteps = pt.xSteps;
    posYSteps = pt.ySteps;
    posAngleSteps = pt.angleSteps;
    elapsedMs += pt.dtMs;
    sendResponse("TRAJ_PROGRESS:" + String(elapsedMs / 1000.0, 4) + ":" +
                 String(pt.xSteps) + ":" + String(pt.ySteps) + ":" +
                 String(pt.angleSteps));

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
  else if (line.startsWith("MANUAL")) handleManual(line);
  else if (line.startsWith("TRAJ_BEGIN")) handleTrajBegin(line);
  else if (line.startsWith("TRAJ_POINT")) handleTrajPoint(line);
  else if (line.startsWith("TRAJ_END")) handleTrajEnd();
  else if (line.startsWith("RUN")) handleRun();
  else if (line.startsWith("PAUSE")) handlePause();
  else if (line.startsWith("RESUME")) handleResume();
  else if (line.startsWith("ABORT")) handleAbort();
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
        // RESUME. If inputLine were cleared only after processLine()
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
