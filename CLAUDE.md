# Gait Simulator Control — Project Context

## Project
Raspberry Pi 5 high-level controller for a 3-DOF biomechanical gait
simulator. ESP32 handles real-time motor control; Raspberry Pi handles
UI, orchestration, and data. Communication: USB Serial, 115200 baud,
plain ASCII line protocol. RPi is master, ESP32 is slave.

## Environment
- Raspberry Pi OS (Bookworm), Python 3.13, PySide6 (NOT PyQt6 — chosen
  for LGPL licensing, future tech-transfer flexibility).
- Enter dev env: `gaitsim` (alias for `cd ~/Projects/gaitsim-control &&
  source venv/bin/activate`).
- Run app: `python3 main.py` (needs a display session — SSH alone
  won't render the GUI; use VNC or the touchscreen directly).
- ESP32 test firmware: separate PlatformIO project on the dev laptop,
  simulates motor behavior (no real motors yet). Protocol-compliant.

## Assets (media files — video, and future model/image assets)
Convention (enforced by `.gitignore`, `assets/*/source/`): raw/uncompressed
originals (camera exports, uncompressed renders) go in a sibling `source/`
folder next to the asset type — e.g. `assets/videos/source/`, a future
`assets/models/source/` — and are NEVER committed (too large, and git
history on this repo is already fragile, see Known pre-existing issue
below). Only the compressed/optimized derivative actually used by the app
is tracked, living directly in `assets/<type>/`. Example: the welcome
screen's looping video —`assets/videos/source/MOTION360.mp4` (raw, 1.2GB,
gitignored) was transcoded to `assets/videos/motion360_welcome.mp4` (H.264,
735KB, tracked); see the ffmpeg command in `welcome_screen.py`. Before
adding any new large media file, transcode it into this layout rather than
committing the raw export.

## Installed skills — project-specific caveats
- `frontend-design` (global skill, installed for other projects too):
  assumes HTML/CSS/JS output by default. In THIS project there is no
  web frontend — apply its design principles (bold intentional
  direction, avoiding generic look-and-feel, aesthetic decisiveness)
  to PySide6 widgets/QSS/pyqtgraph styling instead. Never generate
  HTML/CSS/JS output here.

## Architecture (strict layering — DO NOT collapse these)
src/communication/protocol.py       — message format only, no I/O
src/communication/serial_manager.py — raw transport, threaded reader, no protocol knowledge
src/communication/esp32_controller.py — protocol-aware, blocking calls w/ timeout + async callbacks
src/controllers/system_state.py     — SystemStateMachine: single source of truth for app state,
enforces business rules (e.g. no manual move while RUNNING)
src/ui/bridge.py                    — StateMachineBridge(QObject): translates SystemStateMachine
callbacks (fired on serial thread) into Qt Signals (thread-safe)
src/ui/style.py                     — centralized touch UI constants (sizes, fonts)
src/ui/main_window.py               — owns the ONE shared ESP32Controller/StateMachine/Bridge,
QStackedWidget navigation, screens know nothing of each other
src/ui/screens/connection_screen.py — connect, home, manual per-axis movement
src/ui/screens/trajectory_screen.py — select/load/send trajectory, Run/Pause/Resume
src/utils/trajectory_loader.py      — CSV -> List[TrajectoryPoint], positional column mapping
(first 4 cols = time,pos_x,pos_y,angle; sep=";")
src/utils/trajectory_library.py     — lists/resolves trajectories by id in data/trajectories/
## Hard rules (violating these breaks the design intent)
1. UI screens NEVER call ESP32Controller directly — always go through
   SystemStateMachine (via the shared bridge), which enforces
   can_home()/can_move_manually()/can_run()/etc.
2. SystemStateMachine and everything below it has ZERO Qt dependency.
   Qt only enters at src/ui/bridge.py and below.
3. Blocking ESP32 calls (home, move_manual, run, send_trajectory) run
   on a background QThread (`_ActionWorker` pattern, duplicated per
   screen currently — promote to shared module if a 3rd screen needs it).
4. Trajectory CSVs live in `data/trajectories/`, one .csv per file,
   filename (no extension) = identifier. Naming convention:
   `<phase>_<version>.csv` (e.g. `balanceo_v4.csv`).
5. Protocol spec is authoritative in `docs/protocol.md` — any firmware
   (test or definitive) must comply with it; don't change wire format
   without updating that doc.
6. `firmware/Codigodemicompaneroparaelesp32.cpp` is a teammate's
   in-progress DEFINITIVE ESP32 firmware (drives the real 3 motors) —
   NOT the test firmware, and not final despite living under
   `firmware/`. It is being actively modified by them elsewhere.
   STRICT rule: never edit this file's actual code. Any suggested
   change must be added as a comment at the relevant line (not applied),
   and a summary block appended at the END of the file listing every
   suggested change and its line number, so it can later be reconciled
   with docs/protocol.md and our RPi-side code. This integration pass
   is deliberately deferred — do not start it until explicitly told to
   (see Next planned step).
   ## State machine states
DISCONNECTED → HOMING → IDLE ⇄ RECEIVING_TRAJECTORY → IDLE → RUNNING ⇄ PAUSED → IDLE (on FINISHED)
Manual movement and Home only allowed from IDLE (Home also allowed
from DISCONNECTED, i.e. first boot).

## Team
- Luis (me): architecture owner, integrates all modules, writes thesis.
- Leonardo Machiavello: wrote trajectory_loader.py (had to fix his
  import path, validation order, sep=";" bug). Currently unavailable
  for new tasks — do not assume he'll pick up pending work.

## Status as of last session (2026-07-19)
Horizon 1 core functionality DONE and verified end-to-end on real hardware
(real Raspberry Pi + real test ESP32), including:
- Fixed FINISHED-label bug: SystemStateMachine.on_trajectory_finished was
  assigned by bridge.py but never invoked; added the callback + its call in
  system_state.py._on_finished().
- Added live plotting (pos_x, pos_y, angle vs time) in trajectory_screen.py
  using pyqtgraph (GraphicsLayoutWidget, 3 stacked linked plots, titled,
  colored per dataviz-skill-validated palette, units cm/deg). Data source is
  a NEW unsolicited protocol message `TRAJ_PROGRESS:<t>,<x>,<y>,<angle>`
  (documented in docs/protocol.md as provisional/test-only), emitted once per
  point by firmware/gaitsim-esp32-test/main.cpp's handleRun() at a fixed
  120ms test-only cadence. Full chain updated: protocol.py (parse), 
  esp32_controller.py (on_trajectory_progress callback), system_state.py
  (forwards, no state change), bridge.py (trajectory_progress Qt signal).
- Plot behavior: freezes on PAUSE, resumes on RESUME (no code needed for
  this — falls out of TRAJ_PROGRESS simply pausing/resuming), stays visible
  after FINISHED (no longer auto-clears), new "Limpiar Gráfica" button clears
  it manually. Clears automatically on a fresh Run.
- Fixed a real Pause/Resume firmware bug (main.cpp): handleRun()'s blocking
  for-loop never read Serial, so PAUSE/STOP sent mid-run sat unprocessed
  until the run finished. Restructured so handleRun() polls Serial during
  the per-point wait and blocks in-place (same call, same for-loop) while
  PAUSED, so RESUME truly continues from where it left off.
- Fixed a second firmware bug this introduced: pollSerial()'s shared global
  `inputLine` was cleared AFTER processLine() returned, so a nested
  pollSerial() call (inside handleRun(), reading a command sent mid-run)
  could concatenate onto the still-populated buffer (e.g. "RUN"+"PAUSE" ->
  "RUNPAUSE", which startsWith("RUN") and wrongly re-triggered handleRun(),
  eating the real PAUSE and requiring a second tap). Fixed by capturing a
  local copy and clearing the global before dispatch.
- Firmware file was a one-time authorized exception to the "read-only
  reference" rule — Luis reflashes it himself via PlatformIO on his laptop.

Known pre-existing issue (unrelated to this session's work, NOT fixed):
the git repo has multiple corrupted/missing objects under .git/objects/
(git fsck fails on many blobs). Working tree files are unaffected and
`git status`/edits work fine, but commit history integrity is suspect.
Flagged to Luis; no repair attempted (backup .git before trying anything).

## Next planned step (as of 2026-07-20)
"Professional interface" pass DONE: dark "instrument panel" theme
(APP_STYLESHEET in style.py, applied in main.py), branded welcome/idle
screen with tap-to-continue (welcome_screen.py) including a looping preview
of assets/videos/motion360_welcome.mp4 (compressed derivative — see Assets
section above), segmented-control nav bar, card-styled QGroupBox sections,
primary/secondary button variants. Confirmed by Luis ("se ve perfecto");
data/positions/pruebo2.json changed during this session in a way consistent
with real hardware testing (x: 12.5->8.5), though no explicit verbal
hardware confirmation was given — flagging per Confirmation protocol rather
than assuming.

Review pass on `firmware/Codigodemicompaneroparaelesp32.cpp` also DONE
(comment-only, see Hard rule 6 and the file's own end-of-file REVIEW
SUMMARY block) — found it has no serial command dispatch wired up yet
(empty loop()), a different binary command protocol + stance/swing
trajectory model than docs/protocol.md, no step<->cm/degree conversion,
no PAUSE/RESUME or TRAJ_PROGRESS equivalent, and one likely copy-paste bug
(CMD_SET_OFFSET_SWING writing to *_stance_ variables). Luis's explicit
choice: PAUSE this Horizon 2 thread (do not resume without explicit
instruction) and continue RPi-side functionality instead while the
teammate keeps developing their firmware.

Implemented (2026-07-20, later same session): safe inter-trial
repositioning + retry flow, addressing a real physical safety
requirement (never let Y drop below the previous trial's initial Y
during automatic repositioning — a prosthesis may be mounted there).
- New protocol command `ABORT`/`ABORTED` (docs/protocol.md,
  protocol.py, esp32_controller.py) — abandons a PAUSED trajectory,
  distinct from the existing resumable `STOP`. Added to the TEST
  firmware (firmware/gaitsim-esp32-test/main.cpp) via `handleAbort()`;
  reuses handleRun()'s existing abort-the-loop path, no new firmware
  state needed. Also fixed a real gap there: `handleRun()` never
  updated posX/posY/posAngle per point, so GET_POSITION was stale
  after a PAUSE — now updated in step with each TRAJ_PROGRESS.
- `SystemStateMachine.abort()`/`can_abort()` (PAUSED -> IDLE) and
  `safe_return_to_position(target, floor_y)` in system_state.py — the
  5-step safety sequence (lift above max(current_y, floor_y)+5cm,
  capped gracefully if the mechanical limit rejects it; angle -> 0;
  move X; rotate to target angle; only then descend to target Y),
  entirely composed of existing GOTO calls, no new wire primitive
  needed for the movement itself. Verified with a mocked controller
  (3 scenarios: normal case, current-Y-below-floor case, lift-hits-
  limit case) — all passed; NOT yet verified on real hardware.
- trajectory_screen.py: new "Reiniciar Ensayo"/"Elegir Otro Ensayo"
  buttons (enabled only while PAUSED, second row under Run/Pause/Resume
  so it doesn't overflow the column width); the existing "save initial
  position?" prompt after FINISHED now offers a 3-way retry choice
  (same point / new point / different trial) when the operator declines
  to save. New position entry for "otro punto"/"otro ensayo" is
  self-contained in TrajectoryScreen (QInputDialog-based), not routed
  through ConnectionScreen.
Corrected 2026-07-20 (later session) after Luis reflashed and tested on
real hardware — the buttons existed but had no real logic wired yet:
- "Reiniciar Ensayo" (trajectory_screen.py `_on_restart_trial_clicked`)
  now actually reposition-then-resend-then-run automatically (was:
  reposition only, requiring manual Send+Run).
- "Elegir Otro Ensayo" (`_on_choose_other_clicked`) now aborts (if
  paused) and navigates to ConnectionScreen via a new
  `request_new_trial` Signal (MainWindow wires it to
  `_show_connection_screen`, which also fixes up the nav bar's checked
  state since programmatic screen switches don't do that automatically).
  This REPLACED the self-contained position-entry dialogs I'd built
  directly in TrajectoryScreen (removed) — those duplicated
  ConnectionScreen's existing fields/combo for no reason; simpler to
  reuse that screen entirely.
- ConnectionScreen's existing "Ir a Posición Inicial" button
  (`_on_goto_position_clicked`) now itself calls
  `safe_return_to_position()` instead of plain `go_to_position()`
  whenever `position_session.position` already holds a previous
  trial's position (i.e. every use except the very first GOTO right
  after HOME) — this is what "Elegir Otro Ensayo" ultimately relies on.
- `SystemStateMachine.ANGLE_REFERENCE_DEG`: the movement sequence's
  angle-neutralization value was hardcoded as a literal `0.0`; now a
  named class constant, since Luis clarified it represents a limit-
  switch reference to be calibrated later, not literal zero.
Verified offscreen with mocked ESP32Controller calls (not just unit
logic): restart chain calls abort->5-step reposition->send_trajectory->
run in order; choose-other correctly switches screens and nav state;
ConnectionScreen's goto correctly branches plain-GOTO vs the 5-step
safe sequence depending on whether a previous position exists. Still
NOT reflashed/tested on real hardware as of this edit — same
Confirmation protocol rule applies before calling this done.

3D monitor (2026-07-20, later still): built per Luis's choice — Qt3D
(PySide6.Qt3DCore/Qt3DExtras/Qt3DRender, already available, no new
dependency), separate non-modal window opened via a "Monitor 3D" button
in the header (main_window.py `_open_monitor_3d`). New module
src/ui/monitor_3d_window.py: a QCuboidMesh "platform" entity translated/
rotated per polled GET_POSITION (position.x/y -> scene X/Y, angle ->
rotation around scene Z so it tilts like a seesaw), a fixed sphere
marker at the HOME origin as the calibration view, orbit camera. Polls
GET_POSITION every 200ms via a dedicated QThread (`_PositionPoller`)
while the window is visible — there is no position-changed signal for
the intermediate steps of safe_return_to_position(), so polling is how
this can show that sequence actually happening. SCENE_SCALE (cm ->
scene units) is a placeholder, not calibrated to real rig dimensions.

IMPORTANT — verification gap, different from every other UI feature
this session: Qt3D needs a real GPU/GL context. This dev environment's
offscreen test setup has none — `Qt3DWindow.show()` segfaults here
every time (confirmed, not a hang). One real bug was caught and fixed
this way (`Qt3DWindow.setCamera()` doesn't exist; the camera belongs to
`defaultFrameGraph()` instead) via a traceback before the eventual
crash, and the full Qt3D API surface used was verified to exist via
introspection (no show()/render). But the window has NEVER been
visually rendered or seen to actually work — this can ONLY be verified
on Luis's real display. Higher risk of needing a follow-up fix than
anything else built this session; do not assume it works.

Diagnostics added 2026-07-20 (later still) after Luis reported BOTH
issues still failing on real hardware — "ESP32 reported error ABORT" on
Reiniciar Ensayo/Elegir Otro Ensayo, and Monitor 3D "showing nothing":
- Could not find a definitive code bug for the ABORT error by re-reading
  (the RPi/firmware state machines look correctly synchronized on
  paper) — a segfault from Monitor 3D killing the whole process
  mid-paused-trial (RPi state lost, ESP32 left mid-nested-pollSerial())
  is one plausible cross-feature explanation, but unconfirmed.
- Added wire-level + state-transition logging (src/communication/
  serial_manager.py, src/controllers/system_state.py) to the SAME file,
  `logs/gaitsim.log` (gitignored) — every sent/received line and every
  state transition, with millisecond timestamps, so the exact sequence
  around a failure can be read afterward instead of guessed at. Next
  time this reproduces, check that log (or share the relevant excerpt).
- Monitor 3D now pre-flight-checks for a working OpenGL context
  (`QOpenGLContext().create()`) BEFORE attempting to build the Qt3D
  scene, showing a clear dialog instead of risking the crash if it
  fails. This cannot guarantee Qt3D's own RHI backend will succeed even
  when the check passes (different code path) but should at minimum
  stop it from taking down the whole app. If the dialog itself appears
  on real hardware, that's a real, useful, different data point (GL
  genuinely unavailable) vs. some other Qt3D-specific failure.
Both fixes are diagnostic/defensive, not confirmed fixes for the root
cause — the actual bug(s) still need real-hardware evidence.

RESOLVED 2026-07-20 (later still), after Luis's next test round:
- ABORT still failing with UNKNOWN_COMMAND confirmed it's a firmware
  deployment issue (the running ESP32 binary doesn't have handleAbort()
  — the code in this repo is correct, per the earlier mocked tests),
  NOT an RPi-side logic bug. Not something more code changes here can
  fix — Luis needs to confirm firmware/gaitsim-esp32-test/main.cpp is
  actually what his PlatformIO project builds/flashes.
- Real bug found and fixed: `_maybe_offer_save_position()` silently
  returned with NO dialog at all when the position already matched a
  saved file (`_matches_saved_file()` true) — meaning the retry offer
  nested inside that dialog's decline-branch could never fire in that
  case. Root cause of "la ventana... no siempre sale".
- Design change per Luis: retry is no longer dialog-gated at all.
  "Reiniciar Ensayo"/"Elegir Otro Ensayo" (trajectory_screen.py) are
  now enabled directly via `_refresh_controls()` whenever PAUSED OR
  IDLE-with-a-loaded-trajectory-and-known-initial-position (i.e. after
  FINISHED too, not just PAUSED) — no popup involved anymore. The old
  `_offer_retry_choice()` dialog and its call sites were removed
  entirely; `_maybe_offer_save_position()` still runs (still useful)
  but no longer gates anything.
- `SystemStateMachine.can_home()` now only allows HOME once per session
  (`self._homed` flag, reset on disconnect) — re-homing is disallowed
  afterward since it would invalidate the (0,0,0) origin that initial
  positions and safe_return_to_position()'s floor depend on.
  ConnectionScreen's Home button auto-disables via this with no changes
  needed there (`_refresh_controls()` already reads `can_home()`).
- "Reiniciar Ensayo"'s status message ("Regresando a la posición
  inicial...") now stays visible at least 3s before flipping to the
  success message (`_run_action(..., min_duration_ms=3000)`) — without
  this, the test firmware's near-instantaneous simulated GOTO could
  flip it before it's readable.
- Monitor window REBUILT from scratch: Qt3D replaced entirely with
  QPainter (monitor_3d_window.py's `_PlatformView` — same rendering
  path as every other widget in this app, no GPU/GL dependency at all).
  Reason: Luis confirmed Qt3D showed nothing on real hardware either,
  matching this dev environment's segfault — not fixable by guessing
  further. 2.5D side-view: platform as a rotated rectangle, a ground
  shadow to read height, fixed HOME marker. Visually verified via an
  offscreen screenshot (confirmed working, unlike Qt3D which could
  never be screenshotted) — this WILL render on real hardware. Nav
  button relabeled "Monitor Posición" (no longer literal 3D).
Still outstanding for next session: (1) Luis needs to verify his
PlatformIO build actually uses this repo's main.cpp, reflash, and
confirm ABORT works; (2) verify the corrected retry/calibration-lock
behavior and the new Monitor Posición window on real hardware.

Open question for next session: what specific functionality comes next
after this is verified — Luis's direction, not assumption.

Consider addressing the git corruption issue (see below) before it causes
real data loss.

## Deferred / not built yet (do not build unless explicitly asked)
GUI polish (splash screen, branding), user management, pathology
library, automatic reports, Digital Twin, sensor integration beyond
current scope — all explicitly out of thesis scope per prior planning.
## Roadmap (two horizons — for context, not a license to skip confirmation)

**Horizon 1 — CURRENT, with TEST ESP32 firmware (firmware/gaitsim-esp32-test/main.cpp):**
Fully working RPi control app against the simulated test firmware —
connection, homing, manual movement, trajectory select/send/run, and
live plotting (pos_x, pos_y, angle) during RUNNING — all verified on
real hardware (real Raspberry Pi + real test ESP32), not just
code-reviewed. This is the current active goal.

**Horizon 2 — FUTURE, not started, blocked on teammate's work:**
Integration with the DEFINITIVE ESP32 firmware (controls the real
simulator motors), built by a teammate, not yet available. Requires
the same wire protocol (docs/protocol.md) to be honored by that
firmware. Do NOT start any work targeting this horizon — no motor-
specific code, no assumptions about the definitive firmware's
internals — until explicitly told the definitive firmware exists and
is ready to integrate.

Work stage by stage within Horizon 1. For each stage: present the
design plan first, wait for explicit confirmation, implement, then
wait for me to verify on real hardware before starting the next stage.
Do not chain multiple stages autonomously even if the roadmap above
makes the next step obvious.

## Confirmation protocol (optimized for token efficiency)

Only pause for my confirmation before/after LARGE changes — a new
screen, a new module, a new major feature (e.g. live plotting,
navigation system, a new controller layer). Small internal steps
within implementing that one large change (helper functions, minor
refactors, obvious bug fixes within the same file) do NOT need
individual confirmation — batch them into the single large change and
present the result together.

Before asking "should I continue?", always state explicitly WHAT you
are about to do next (the specific large change), not just "continue?".
After implementing a large change, do not mark it done or move to the
next one until I explicitly confirm I tested it manually on real
hardware (Raspberry Pi + real ESP32) and it works correctly. Code that
runs without errors is not the same as verified — wait for my explicit
"it works" before proceeding.

## Long-term vision (context only — do NOT build ahead of Horizon 1)

The end goal beyond Horizon 1/2 above is a professional-grade touch
interface (branded splash screen, polished UI/UX) evolving into a
modular research platform for the lab, eventually supporting:
user management, a trajectory library UI, a pathology library,
automated test protocols, a Digital Twin synced to the physical
simulator, additional sensor integration, automatic report generation,
and a plugin-style architecture so other students can add modules
without touching the core. This vision informs naming/structure
choices (e.g. keep screens decoupled, keep protocol.py hardware-
agnostic) but NONE of these features are in scope now — only build
them if explicitly requested, one at a time, after Horizon 1 is fully
verified.