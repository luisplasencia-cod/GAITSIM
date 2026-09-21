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
src/utils/trajectory_generator.py   — generates (doesn't load) a synchronized (0,0,0)->target
trajectory as List[TrajectoryPoint], validated against CalibrationSpace; sent/run through the
SAME TRAJ_BEGIN/TRAJ_POINT/TRAJ_END+RUN protocol as any CSV trajectory, no new wire commands
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
RESOLVED 2026-07-31: both outstanding items above confirmed on real
hardware — Luis reflashed with a PlatformIO build matching this repo's
main.cpp and ABORT no longer errors with UNKNOWN_COMMAND; the
corrected retry/calibration-lock behavior and the Monitor Posición
window were also confirmed with no visual problems. This was given as
one broad confirmation covering the whole feature set built since
2026-07-19 together (see the 2026-07-31 status block at the end of
this section for what that does and doesn't cover, and for several
features built 2026-07-22 through 2026-07-25 not otherwise narrated
in this file).

Open question for next session: what specific functionality comes next
— Luis's direction, not assumption.

Consider addressing the git corruption issue (see below) before it causes
real data loss.

Git history note (2026-07-22): the corruption above was hit for real
while linking this repo to GitHub (`github.com/luisplasencia-cod/GAITSIM`,
remote `origin`, branch `main`) — 7 blobs from two old commits
(`a7b28f3`, `ead9800`) were unrecoverable 0-byte objects. Per Luis's
choice, rebuilt as a single clean root commit from the then-current
working tree (old detailed commit history discarded from the active
repo; full pre-rebuild `.git` backed up locally at
`~/Projects/gaitsim-control-git-backup-20260722-231537`, not in the
repo itself). Added `README.md` at the repo root as the onboarding
entry point for teammates (architecture overview + per-folder guide +
a "want to change X -> go to Y" table) — keep it in sync with this
file's Architecture section when either changes.

Implemented (2026-07-23): synchronized (0,0,0) -> initial-position
trajectory, replacing the previous instantaneous GOTO jump right after
calibration. Design choice: reuse the EXISTING TRAJ_BEGIN/TRAJ_POINT/
TRAJ_END + RUN protocol (docs/protocol.md) instead of adding any new
wire command — this generated approach trajectory is sent and executed
exactly like a CSV-loaded gait trajectory, so it gets TRAJ_PROGRESS,
PAUSE/RESUME/ABORT, and the test firmware's existing progressive
simulation (handleRun()) entirely for free. Firmware: NO changes needed.
- New `src/utils/trajectory_generator.py`:
  `generate_synchronized_trajectory(target, calibration_space)` — all 3
  axes (X, Y, angle) start at t=0 and reach `target` at the SAME t=T
  (straight line in configuration space), where T is set by whichever
  axis takes longest at its own assumed max speed
  (`SPEED_X_CM_S`/`SPEED_Y_CM_S`/`SPEED_ANGLE_DEG_S`, sampled every
  `WAYPOINT_INTERVAL_S` — all placeholders pending real rig
  calibration, same spirit as `system_state.py`'s existing
  `Y_LIFT_MARGIN_CM`/`ANGLE_HORIZONTAL_OFFSET_DEG`). Raises
  `PositionOutOfRangeError` if `target` falls outside
  `SystemStateMachine.CalibrationSpace` on any axis — validated BEFORE
  anything is sent to the ESP32. Target indistinguishable from the
  origin -> empty list, treated as a no-op (no wire traffic).
- `connection_screen.py`: `_on_goto_position_clicked`'s `previous is
  None` branch (the very first "Ir a Posición Inicial" after HOME) now
  calls new `_on_goto_initial_synchronized()` — validate, generate,
  `send_trajectory()` then `run()` — instead of plain `go_to_position()`.
  Everything else (`safe_return_to_position()` for later
  repositioning between trials) is UNCHANGED — different feature,
  different safety concern (not letting Y drop below a mounted
  prosthesis), out of scope here. Since `run()` only confirms RUNNING
  and the real completion (FINISHED) arrives later via the shared
  bridge `trajectory_finished` signal — which ALSO fires when
  TrajectoryScreen's own gait-trajectory runs finish — added a guard
  flag `_awaiting_initial_move` (+ `_pending_initial_position`) so
  ConnectionScreen only reacts to its own move, not an unrelated one;
  cleared on success, on a mid-move device error, and on disconnect
  (none of those emit `trajectory_finished`, since
  `SystemStateMachine._on_device_error`/`_on_disconnected` fall back to
  IDLE/DISCONNECTED without it).
- `calibration_map_window.py`: `CalibrationMapView` gained
  `set_current_position(Position | None)` — draws a live marker (dot +
  short line for orientation, using the same 90-angle Qt-arc mapping
  already used for the calibration arc) on top of the existing
  footprint rectangle/arc. `CalibrationMapWindow` feeds it from the
  bridge's `trajectory_progress` signal — deliberately NOT scoped to
  only this new feature, so the same marker also tracks a normal gait
  trajectory's progress if the window happens to be open during a Run.
Verified offscreen with a mocked controller (not just unit logic): full
send->run->async-FINISHED chain confirmed `InitialPositionSession` ends
up with the right position and the guard flag correctly ignores an
unrelated `trajectory_finished`; out-of-range target confirmed to be
rejected with NO wire traffic; `CalibrationMapWindow` with a live
marker rendered via an offscreen screenshot without crashing (marker
position/orientation visually correct).

Also implemented 2026-07-22 through 2026-07-25 (full detail in the
project's memory files, not re-narrated here): the `<HOME>` calibration
sweep now maps the available movement space per axis
(`CalibrationMapWindow`/`CalibrationMapView`, angular axis reinterpreted
relative to horizontal via `ANGLE_HORIZONTAL_OFFSET_DEG`); a central
`src/utils/trajectory_validator.py` rejects any out-of-range move/
trajectory before it reaches the ESP32, surfaced via a new
`LimitViolationDialog` and a fixed-height "Estado del Sistema" box;
the live trajectory plot no longer mixes in manual/return/abort
movement (`_plot_active` gating in `trajectory_screen.py`), and `Run`
now stays blocked until a fresh CSV is loaded for the current initial
position; `monitor_3d_window.py` was renamed `platform_view.py` and
folded into `TrajectoryScreen` alongside Trayectorias and the Live
Plot (Monitor Posición is no longer a separate window); and a bug
where a loaded ensayo's `Run` jumped the angle to an absolute CSV
value instead of starting from the rig's current angle was fixed in
`TrajectoryScreen._offset_points`.

**Status as of 2026-07-31**: Luis confirmed real-hardware verification
(real Raspberry Pi + real test ESP32) of the whole feature set above —
calibration mapping, boundary validation, plot isolation/fresh-CSV
gating, the consolidated TrajectoryScreen layout, the angle-offset fix,
and the synchronized initial-position trajectory (including the
marker-tracking check called out above) — no visual problems reported.
Given as one broad confirmation, not itemized per sub-behavior; if a
specific edge case one of these features was built to fix resurfaces
(e.g. an asymmetric calibration range, a CSV whose first angle differs
from the current one, real-finger ergonomics on the 28px `SLIM`
buttons), treat it as not actually covered here rather than a
regression. Horizon 2 remains not started — this confirmation is
scoped to the RPi app + test firmware only.

Also done same day: removed the `MOVE_REL` wire command (real-unit
relative move) as dead code — `SystemStateMachine.move_relative()`
already stopped calling it in favor of a generated single-axis
trajectory (see 2026-07-23 entry above), so the manual-jog buttons were
unaffected; removed from `docs/protocol.md`, `protocol.py`,
`esp32_controller.py`, and the test firmware. And changed
`ConnectionScreen`'s "Ir a Posición Inicial" (both the
`safe_return_to_position` and synchronized-trajectory branches) to
navigate to the monitor screen as soon as the move starts rather than
once it finishes, so its live position polling is visible for the
whole move. Both changes confirmed on real hardware same day, no
problems reported.

**Status as of 2026-08-31**: several protocol changes landed, plus an
explicit pivot away from the test firmware — see below.

HOME simplified (session 1, this machine): the ESP32 no longer streams
6 live `<LIM*MIN/MAX>` events during the calibration sweep — it reports
all 4 limits it doesn't already know to be 0 in a single response,
`READY:xmax:ymax:amin:amax` (raw steps, no more '<' '>' framing
exception at all). Updated: `protocol.py` (`HomeLimits`,
`parse_home_limits`), `esp32_controller.py` (`home()` now returns the
limits instead of firing a per-event callback), `system_state.py`
(`CalibrationSpace` built directly from that return value),
`bridge.py` (removed the now-gone `calibration_limit` signal),
`connection_screen.py`/`calibration_map_window.py` (per Luis's choice:
a single generic "Calibrando..." message during HOMING replaces the
old per-axis live text), and `firmware/gaitsim-esp32-test/main.cpp`.
`docs/protocol.md` rewritten to match, and its placeholder notation
unified (`POSITION:x:y:angle`, not `POSITION:<x>:<y>:<angle>` — no
wire-format placeholder anywhere actually uses literal `<>` except
command framing itself).

Also this session: the teammate's definitive-firmware project (a full
PlatformIO project, not just one file) was uploaded via zip and added
as `firmware/platformIO_control_trayectoria/` (source only — `.pio`
build artifacts and `.vscode` excluded via its own `.gitignore`, to
avoid repeating the git-corruption-from-large-blobs issue below). The
OLD single-file `firmware/Codigodemicompaneroparaelesp32.cpp` (Hard
rule 6) was left untouched, unreconciled with the new project — both
now exist side by side.

Separately, from the Raspberry Pi (same day, different session):
`HOME` no longer has a timeout at all (was 20s) — real calibration
takes far longer than the test firmware's simulated sweep, so it now
waits indefinitely for `READY`. `TRAJ_POINT` split into a TIMED form
(4 fields incl. `dt_ms` — ONLY the CSV/gait ensayo, whose recorded
timing is real) and an UNTIMED form (3 fields, no `dt_ms` — initial
position, joystick, safe return between trials; the ESP32 picks its
own speed), since the definitive firmware treats `dt_ms<=0` as a hard
failure and the RPi's placeholder speed constants for non-gait moves
were never real timing anyway. The leading `t=0` point is no longer
sent as a `TRAJ_POINT` at all (it would always be a zero-duration
delta against the RPi's own already-t=0 baseline — real mismatch vs.
the ESP32's actual position is still captured in the first point that
IS sent). `docs/protocol.md`, `system_state.py`, `protocol.py`,
`esp32_controller.py`, `trajectory_generator.py`,
`platformIO_control_trayectoria/src/communication.h` (missing trailing
`\n` in `communication_tx`, fixed) all updated together.

Verified `docs/protocol.md` against the RPi-side implementation after
pulling the above — accurate. One real gap found and fixed:
`firmware/gaitsim-esp32-test/main.cpp` hadn't been updated for the new
untimed `TRAJ_POINT` form and was rejecting those moves with
`ERROR:MALFORMED`; now accepts both forms (untimed `dt_ms` treated as
0, harmless — only feeds `TRAJ_PROGRESS`'s `t`, and those moves aren't
plotted anyway).

Also found, but per Hard rule 6 (and Luis's explicit instruction below)
NOT touched: the teammate's definitive-firmware upload
(`platformIO_control_trayectoria/src/main.cpp`) implements `TRAJ_POINT`
completely differently from `docs/protocol.md` — it expects a leading
INDEX parameter and uses it as an array subscript
(`gait_dt_ms[rxPacket.parameters[0]] = rxPacket.parameters[1]`, etc.),
which the RPi never sends. It also implements `GOTO`/`GOTO_STOP`/
`TRAJ_STATUS`, which the doc explicitly says not to implement. Not
reconciled — see pivot below.

**PIVOT (Luis's explicit instruction, 2026-08-31)**: as of today, stop
testing against/maintaining `firmware/gaitsim-esp32-test/` — Luis is
moving to testing against the teammate's real/definitive firmware
instead. HOWEVER the `platformIO_control_trayectoria/` copy currently
in this repo is itself known-STALE (not the teammate's latest, per
Luis) — do NOT treat it as a reference, do NOT start the Horizon 2
comment-annotation review pass (Hard rule 6) or any other firmware-side
work, until Luis explicitly says he has an updated copy. Until then,
the only source of truth for "what's correct" is `docs/protocol.md` +
the RPi-side code (`protocol.py`/`esp32_controller.py`/
`system_state.py`) — verify against those only.

**Status as of 2026-09-01**: four protocol/RPi-side additions this
session, ALL still code-only — no real-hardware verification yet
(nothing to reflash against, see PIVOT confirmation below):
1. A configurable time-scale factor (`QDoubleSpinBox`, default 30x, no
   upper cap) in `trajectory_screen.py`'s TRAJECTORY SELECTION box —
   the simulator can't reproduce real gait timing yet, so the CSV
   ensayo's own `t` values are stretched by this factor at load time
   (`_on_send_clicked`), before offset/validation/send. Only affects
   the TIMED ensayo; untimed sends are untouched.
2. `<TRAJ_STATUS>`: the RPi now actively polls this every 1s while
   RUNNING (starting right after `RUN`/`RESUME` confirms `RUNNING`,
   stopped by `PAUSE`/`ABORT`), as a robustness backstop for a lost/
   corrupted unsolicited `FINISHED` — same words (`RUNNING`/`PAUSED`/
   `FINISHED`) reused, no new response shape. Lives in
   `ESP32Controller` (background thread, generation-counter-guarded, no
   `thread.join()` to avoid a self-join deadlock) so it applies to
   every trajectory type with no `SystemStateMachine`/UI changes.
3. `TRAJ_BEGIN`/`RUN` now carry a `tipo` (`1`=CON tiempo, `2`=SIN
   tiempo) — lets the firmware know in advance which `TRAJ_POINT` shape
   to expect instead of inferring it, and `RUN` cross-checks its own
   `tipo` against the `TRAJ_BEGIN` that stored the trajectory
   (`ERROR:TYPE_MISMATCH` on mismatch). `SystemStateMachine` remembers
   the last `send_trajectory(timed=...)` value (`_last_trajectory_timed`)
   and threads it through to `run()` automatically.
4. `TRAJ_POINT` now carries its own 0-based `index` as the FIRST field
   (before `dt_ms`/`dx`/`dy`/`dangle`), matching `TRAJ_BEGIN`'s
   `n_points` — a robustness cross-check independent of the total-count
   check `TRAJ_END` already does (`ERROR:POINT_INDEX_MISMATCH` on a
   mismatch). Distinct from `ACK:index`, the ESP32's own pre-existing
   response.

All 4 verified OFFSCREEN only (mocked serial transport / mocked
controller — no real ESP32), same spirit as prior sessions' "verified
with a mocked controller" entries: wire-format strings, poll cadence,
no-double-fire on a race between the two FINISHED-detection paths,
pause/resume stopping/restarting the poller, and `timed` propagating
end-to-end from `send_trajectory()` to `run()`.

`docs/protocol.md` updated to match (new "Cambio 2026-09-01" entries,
all 3 wire examples in the Trayectorias section updated).

**PIVOT CONFIRMED STILL IN EFFECT**: Luis confirmed this session that
`firmware/gaitsim-esp32-test/` is still NOT being tested against or
flashed (the 2026-08-31 PIVOT above stands) — so `main.cpp` WAS updated
to implement all 4 changes above (for protocol-completeness/
consistency, and because it also fixed a real pre-existing gap:
`handleTrajPoint()` never actually supported the untimed 3-field
`TRAJ_POINT` form despite `docs/protocol.md` documenting it since
2026-08-31), but treat those firmware edits as DORMANT — written to
match the spec on paper, never reflashed or exercised this session.
Do not treat this file as verified, and do not read its presence/
correctness as evidence the protocol works on real hardware.

Also folded into this session (found already on disk mid-session, not
authored here, but pushed together and undocumented anywhere until
now): `SystemStateMachine.MAX_SPEED_X_CM_S`/`_Y_CM_S`/`_ANGLE_DEG_S` +
`TrajectorySpeedExceededError` — a hard per-axis speed ceiling enforced
on every point of TIMED sends only (the CSV/gait ensayo), added after a
REAL Y-axis overspeed incident on hardware (~90 cm/s at the default
30x scale, ~2700 cm/s at 1x — see that exception's own docstring for
the root cause: a stale `InitialPositionSession` offset landing in the
ensayo's timed first point). `TrajectoryScreen` shows the resulting
`(x, y, angle)` max speed after every Load&&Send/Reiniciar Ensayo
attempt (red if any axis exceeded its limit, even though the send was
already rejected in that case). Values are placeholders pending real
rig calibration, same caveat as `STEPS_PER_CM_X`/etc.

Git note: this session's local commit diverged from `origin/main` (2
commits landed there from a 2026-08-31 session: a simpler firmware-only
untimed-`TRAJ_POINT` fix, and the PIVOT documentation above). Merged
via `git merge` (not rebase/force) — one real conflict in
`firmware/gaitsim-esp32-test/main.cpp`'s `handleTrajPoint()` (both
sides touched it), resolved by keeping this session's version since it
was a strict superset (index + tipo, on top of the same untimed
support). Pushed as merge commit `eeabb63`.

**Implemented (2026-09-08), RPi-side only, code-only — NOT yet
verified on real hardware**: "Reiniciar Ensayo" (`trajectory_screen.py`)
can now repeat the same ensayo N times instead of just once, per Luis's
request. Design confirmed with Luis before implementing (per this
file's Confirmation protocol): reuses the SAME button rather than
adding a new one.
- `_on_restart_trial_clicked` now first asks the count via
  `QInputDialog.getInt` (the "floating window" Luis asked for; 1 = same
  behavior as before, unchanged, no dialog-driven regression for the
  common case).
- The actual reposition→resend→run sequence (previously the whole body
  of `_on_restart_trial_clicked`) was extracted into `_restart_trial()`,
  called once directly and then automatically re-called from
  `_on_trajectory_finished` for each remaining leg — chaining happens
  ONLY on a real `FINISHED` (an abort, e.g. via Pause -> "Elegir Otro
  Ensayo", never fires that signal, so pausing already halts the chain
  with no extra state check needed). `info_label` shows `(current/N)`
  progress during the sequence.
- New `_repeats_remaining`/`_repeats_total` on `TrajectoryScreen`, reset
  by new `_cancel_repeat_sequence()` on anything that should stop the
  chain: a failed leg (new `on_failure` param threaded through
  `_run_action`), a device error, a disconnect, or "Elegir Otro Ensayo".
- Per Luis's explicit choice: the existing "¿guardar posición inicial?"
  offer (`_maybe_offer_save_position`) now fires only once, after the
  WHOLE sequence completes (or is cancelled) — not after every leg.
- Verified only via `py_compile` + manual trace of the chaining logic —
  same Confirmation protocol as every other feature in this file: do
  not treat this as done until tested on real Raspberry Pi + real
  ESP32 and explicitly confirmed.

**Fixed (2026-09-08, separate session): real bug making device errors
feel like "the app closes and I have to recalibrate."** Root cause was
in `bridge.py`, not in any error-handling logic: `StateMachineBridge.
__init__` was directly reassigning `state_machine.controller.on_error`/
`on_disconnected` to its own Qt-signal-emitting handlers. Since those
are single-callback attributes (not a list) and `SystemStateMachine.
__init__` had ALREADY claimed them for itself one line earlier (to run
its own recovery — fall back to IDLE on an error, mark DISCONNECTED +
require re-HOME on a real disconnect), the bridge's assignment silently
replaced that handler instead of adding to it. Net effect: on ANY
unsolicited ESP32 error, `SystemStateMachine._state` stayed stuck at
whatever it was (e.g. RUNNING) forever — every `can_home()/
can_move_manually()/can_run()/etc.` then stayed locked against that
stale state, so nothing in the app worked afterward. Luis's only way
out was to close and relaunch, which re-initializes `_homed = False`
and throws away the real HOME calibration for no actual reason (the
ESP32 itself was still fine — never actually disconnected).
- Fix: `SystemStateMachine` now exposes its own `on_device_error`/
  `on_disconnected` callback attributes (same pattern already used
  correctly for `on_trajectory_finished`), fired at the END of its own
  `_on_device_error`/`_on_disconnected` — i.e. AFTER its internal
  recovery already ran, not instead of it. `bridge.py` now subscribes
  to THOSE two attributes rather than ever touching
  `controller.on_error`/`on_disconnected` directly.
- Confirmed via a mocked-controller offscreen script (no real ESP32):
  an unsolicited error now correctly falls `SystemStateMachine.state`
  back to IDLE, leaves `_homed` untouched (`can_home()` stays False,
  no forced re-HOME), and the operator can keep operating (manual
  move, resend, run again) right away — while a REAL disconnect still
  correctly moves to DISCONNECTED and requires re-HOME (unavoidable:
  the ESP32 itself lost its reference in that case). Bridge's Qt
  signals (`device_error`/`disconnected`) still fire exactly as
  before in both cases — only the internal state recovery was broken,
  not the UI notification.
- Added `src/ui/device_error_dialog.py` (`DeviceErrorDialog` +
  `show_device_error()`): a non-modal, reused-per-screen dialog shown
  from `connection_screen.py`'s and `trajectory_screen.py`'s existing
  `_on_device_error` handlers (in addition to, not instead of, the
  status-label text they already set) — the raw `code`/`message` plus
  a short plain-language explanation and suggested next step per known
  code (`LIMIT_REACHED`, `INVALID_STATE`, `TYPE_MISMATCH`,
  `POINT_INDEX_MISMATCH`, `POINT_COUNT_MISMATCH`, `UNKNOWN_COMMAND`,
  bare `ERROR`), plus an explicit "no es necesario recalibrar" line —
  accurate for every code currently in `docs/protocol.md`, since none
  of them represent a lost HOME reference (only a real disconnect does,
  which surfaces through the separate "Disconnected" text instead, not
  this dialog). Non-modal on purpose: Luis may be at the physical rig,
  not looking at the screen, when this fires, and dismissing it must
  never block retrying the action right away.
- Verified offscreen only (dialog construction/text via `QT_QPA_
  PLATFORM=offscreen`, plus the mocked-controller script above) — NOT
  yet seen on real hardware. Next real device error on the actual rig
  is the real test: confirm the app stays usable afterward with no
  restart/recalibration needed.

**Implemented (2026-09-14): contact-threshold ("tara") testing
workflow — design confirmed with Luis before implementing (per this
file's Confirmation protocol), code-only, NOT yet verified on real
hardware.** Purpose: Luis manually lowers Y in small increments from a
recorded "no contact" basal reference (the "tara") to find the force
platform's contact threshold for a given ensayo, then presses Run at
that already-touching Y — running straight from there would start the
gait trajectory (and its force trace) with contact already established.
- New `src/controllers/tara_library.py` (mirrors `position_library.py`'s
  convention): one JSON file per trajectory id under `data/tara/`,
  `{"tara": {x,y,angle,timestamp}, "pruebas": [{y,timestamp}, ...]}`.
  `save_tara()` always overwrites unconditionally (the "corrección
  futura" flow Luis asked for) but preserves existing `pruebas`;
  `add_prueba()` appends one contact-test record.
- `trajectory_generator.generate_detach_trajectory(current, tara_y,
  lift_above_tara_cm, x_shift_cm, calibration_space)`: 2 synchronized
  legs (lift `tara_y + 1cm` + shift `-1cm` in X together, then shift
  back + descend together — "en el mismo tiempo" per Luis's spec),
  starting and ending at the EXACT same `current` position, so the
  platform re-arrives at the contact point already in motion instead
  of the run starting cold. Same generation style as
  `generate_safe_return_trajectory` (graceful Y/X capping against
  `CalibrationSpace`, `current` itself not re-validated). Sent/run
  through the SAME existing untimed `TRAJ_BEGIN/TRAJ_POINT/TRAJ_END+RUN`
  protocol — no wire/firmware changes.
- `SystemStateMachine.perform_pre_run_detach(tara)` (new
  `DETACH_LIFT_ABOVE_TARA_CM`/`DETACH_X_SHIFT_CM` constants, both fixed
  at `1.0`cm per Luis's explicit choice, not UI-configurable) — thin
  wrapper generating + running the detach trajectory via the existing
  `_run_trajectory_blocking()`, same IDLE-only gate and
  `on_trajectory_finished` suppression as `safe_return_to_position()`.
- `trajectory_screen.py`: new "TARA (REFERENCIA BASAL)" box (Tara
  button + status label) next to Load && Send — records
  `position_session.position` (kept in sync by manual jog, see
  `InitialPositionSession.apply_manual_delta`) as the tara for the
  loaded CSV, confirming before overwriting an existing one. `Run` and
  `Reiniciar Ensayo` (every leg of a repeat sequence, not just the
  first — Luis's explicit confirmation) now check for a tara on the
  loaded ensayo: if present, `perform_pre_run_detach()` runs FIRST,
  the landed-back-at Y is logged via `add_prueba()`, then the ensayo is
  RE-SENT (the detach's own `TRAJ_BEGIN` overwrites whatever Load &&
  Send had stored) before `run()`. No tara on record -> unchanged
  behavior (opt-in per CSV, confirmed with Luis).
- New `src/ui/screens/tara_history_screen.py` (`TaraHistoryScreen`),
  reachable via a new "Pruebas" nav bar entry (`main_window.py`, 3rd
  `QStackedWidget` page) — read-only table, one row per prueba across
  every ensayo, refreshed on show. "Exportar a Excel (.xlsx)" button
  (on-demand, per Luis's choice over `.csv`) via `pandas.DataFrame.
  to_excel(engine="openpyxl")` — added `openpyxl` to `requirements.txt`
  (pandas was already a dependency). Purely file-based
  (`tara_library.py` directly), no bridge dependency.
- `docs/protocol.md`: unchanged — no new wire command, same reasoning
  as `safe_return_to_position()`/the synchronized initial-position
  trajectory before it.
- Verified OFFSCREEN only, same "mocked controller, not just unit
  logic" depth as prior sessions' entries: `generate_detach_trajectory`
  geometry (starts/ends exactly at `current`, reaches the expected
  lifted/shifted waypoint, Y/X capping against `CalibrationSpace`);
  `tara_library` round-trip (save/load/add_prueba/overwrite-preserves-
  pruebas/load_all); `perform_pre_run_detach()` against a mocked
  `ESP32Controller` (async-`FINISHED` simulated via a background
  timer, matching real hardware's callback ordering) — confirms exactly
  one trajectory sent, `run()` called untimed, state back to IDLE, and
  `InvalidTransitionError` when not IDLE; `pandas`+`openpyxl` xlsx
  export/read round-trip against the table's exact row/column shape.
  The Qt widgets themselves (Tara button, history screen/table) were
  NOT rendered even offscreen this session (PySide6 unavailable in
  this dev environment) — reviewed by code inspection + `py_compile`
  only. NOT tested on real hardware. Same Confirmation protocol as
  every other feature in this file: do not treat this as done until
  verified on real Raspberry Pi + real ESP32 (in particular: the
  detach sequence's physical direction — Y-up/X-toward-zero — and that
  the ensayo genuinely resumes smoothly after the re-send) and
  explicitly confirmed.

**Implemented (2026-09-18): height-variability/repeatability test
matrix — replaces the "Pruebas" screen entirely (Luis's explicit
choice).** Design confirmed with Luis before implementing (per this
file's Confirmation protocol: talla is read from each ensayo CSV's
filename via the existing "talla<N>" convention, current example CSVs
are NOT the definitive set; success = FINISHED with no ERROR and no
PAUSE/interruption during that specific point). Code-only, offscreen-
verified only (PySide6 IS available in this session, unlike 2026-09-14
— see below) — NOT yet tested on real hardware.
- Matrix shape: 10 columns (talla, 162-180cm step 2cm —
  `VariabilityMatrixScreen.TALLAS_CM`) x 6 rows (depth below tara,
  `variability_library.DEPTH_ROWS_MM = (0, -1, -2, -3, -4, -5)` mm —
  row 0 is the tara itself). Each cell holds up to
  `SAMPLES_PER_CELL=5` repeatability samples.
- `src/utils/trajectory_library.py`: new `parse_talla_cm(trajectory_id)`
  — regex `talla(\d+)` on the id, matching the naming already used by
  every ensayo CSV on disk (e.g. `Control_apoyo_talla162_montaje35`).
- `src/utils/trajectory_generator.py`: new
  `generate_variability_point_trajectory(current, tara, depth_cm,
  calibration_space)` — 2 synchronized legs (current -> tara, then
  tara -> tara.y+depth_cm), same "always approach from the same
  reference" reasoning as `generate_detach_trajectory`'s return leg,
  so repeats/depths stay comparable. Only the final target is
  validated against `calibration_space`. Sent/run through the SAME
  existing untimed TRAJ_BEGIN/TRAJ_POINT/TRAJ_END+RUN protocol — no
  wire/firmware changes, same pattern as every generated trajectory
  before it.
- `SystemStateMachine.perform_variability_point(tara, depth_mm)` (new
  `VariabilityPointResult` dataclass: `y_real`, `success`,
  `interrupted`, `error_code`) — unlike
  `perform_pre_run_detach()`/`safe_return_to_position()`, this does
  NOT just block on `on_trajectory_finished`: it also temporarily
  hooks `on_device_error` (an error never fires
  `on_trajectory_finished`, so blocking on that alone would hang
  forever) and polls `_state` for PAUSED then IDLE-without-FINISHED
  (an abort after a pause ALSO never fires `on_trajectory_finished` —
  see `abort()`'s own docstring) instead of a single blocking wait, so
  a mid-point pause/abort/error is detected and recorded as a failed
  sample rather than hanging the app. Both callbacks are saved/
  restored around the call, same technique `_run_trajectory_blocking()`
  already used for `on_trajectory_finished` alone.
- New `src/controllers/variability_library.py` (mirrors
  tara_library.py's one-file-per-trajectory-id convention, under
  `data/variabilidad/`) — `load_matrix()`/`add_sample()`. One-time
  migration: if no matrix file exists yet but tara_library.py already
  has legacy flat "pruebas" for that id (recorded before this matrix
  existed, always implicitly at the tara itself), they're migrated
  into row 0 (marked successful) so the real samples Luis captured on
  hardware earlier today (talla162, 10 pruebas) don't disappear from
  the app — only persisted to disk if there was actually something to
  migrate.
- New `src/ui/screens/variability_matrix_screen.py`
  (`VariabilityMatrixScreen`) — replaces `tara_history_screen.py`
  (deleted; no longer referenced anywhere) in `main_window.py`'s
  QStackedWidget index 2 / nav button (relabeled "Matriz de Pruebas").
  Unlike the old read-only screen, this one needs the shared bridge
  (executing a point is a real blocking device action, same
  `_ActionWorker`-on-background-thread pattern as TrajectoryScreen).
  Grid cells color-coded (gray=sin datos, neutral=parcial, verde=5/5
  sin fallos, rojo=incluye algún fallo); selecting a cell shows its
  recorded samples and an "Ejecutar punto" button (enabled only while
  idle AND that talla has both a CSV and a tara on record) that runs
  one more sample — the same action re-runs a specific saved point on
  demand, satisfying Luis's "poder seleccionar esa fila/columna y
  volver a ejecutar el ensayo guardado para ese punto". Export to
  Excel reuses the same pandas/openpyxl mechanism as the old screen.
- Verified OFFSCREEN this session (PySide6 available here, unlike
  2026-09-14's dev environment): generator geometry + out-of-range
  rejection; `parse_talla_cm` regex; `variability_library` round-trip
  + legacy migration + invalid-row rejection; `perform_variability_point`
  against a fake controller for 4 scenarios (normal FINISHED, device
  ERROR, pause-then-abort, rejected while not IDLE) confirming the
  right `success`/`interrupted`/`error_code` each time AND that the
  bridge's own `on_trajectory_finished`/`on_device_error` are restored
  afterward (no permanent hook leak, no spurious
  `bridge.trajectory_finished` Qt signal for this internal move); the
  full `MainWindow` constructed with the new screen wired in and
  navigated to via its nav button; an offscreen screenshot of
  `VariabilityMatrixScreen` itself with realistic mixed data (5/5
  success, partial, a failure, an ensayo with no tara, a talla with no
  CSV) — colors/text/detail-panel/samples-table all rendered as
  intended, "Sin CSV"/"Sin tara" shortened to "—" with a tooltip after
  the initial render showed the full text clipping in the narrow
  10-column layout.
- NOT tested on real hardware: whether a real mid-run PAUSE/ABORT is
  actually observed by the polling loop in time (only exercised via a
  simulated state transition here, not a real ESP32 exchange), and the
  whole feature end-to-end per this file's Confirmation protocol.
- Talla CSVs 164/166/168/170/172/174/176/178/180 still not uploaded as
  of this note (Luis's explicit confirmation, 2026-09-18) — only
  162/165/175/185 exist, none matching the matrix's own 2cm-step
  columns exactly except 162. Guide by the "talla<N>" filename
  convention when they arrive; current files are examples, not
  definitive.

**Implemented (2026-09-18, later same session): fused detach+ensayo
trajectory — eliminates the dead pause between the platform-detach hop
and the real ensayo starting.** Luis's explicit request: the gap he
observed between "lift 1cm off tara + shift back/forward" and the
gait trajectory actually starting had to disappear or become
imperceptible. Root cause (confirmed, not guessed): the OLD flow
(`SystemStateMachine.perform_pre_run_detach()`) sent/ran the hop as
its OWN untimed trajectory, blocked until it physically finished, and
ONLY THEN transferred the whole ensayo CSV (TRAJ_BEGIN + every
TRAJ_POINT + ACK + TRAJ_END, potentially hundreds of points over
115200-baud serial) before calling RUN again — that second transfer's
wall-clock time was the visible dead pause, with the platform just
sitting still. `can_send_trajectory()` requires IDLE, so the ensayo
could not be pre-loaded during the hop's own RUNNING window either
(the hop's own TRAJ_BEGIN would have clobbered it anyway).
- Fix: hop + ensayo now travel as ONE continuous trajectory, ONE
  TRAJ_BEGIN, ONE RUN — no second transfer sits between them at all,
  so the gap is eliminated by construction, not just shortened.
- Luis's explicit requirement on HOW: the hop's own timing must use
  the SAME time-scale factor already applied to the ensayo's CSV
  timeline (`TrajectoryScreen.time_scale_spinbox`, default 30x) —
  otherwise the hop (independent placeholder speed) and the ensayo
  (scaled 30x slower) would move at wildly inconsistent speeds despite
  being one continuous motion.
- `trajectory_generator._interpolate()` gained an optional
  `time_scale: float = 1.0` param (multiplies the computed duration;
  default preserves every other caller's exact behavior unchanged —
  synchronized/safe-return/variability-point generation don't pass it).
- `generate_detach_trajectory()` REMOVED, replaced by
  `generate_detach_and_ensayo_trajectory(current, tara_y,
  lift_above_tara_cm, x_shift_cm, ensayo_points, time_scale,
  calibration_space)` — builds the same 2-leg hop as before (now
  honoring `time_scale`), then appends `ensayo_points` directly onto
  the same continuous point list (`_append_phase`, same de-dup as
  every other multi-phase generator here).
- `SystemStateMachine.perform_pre_run_detach()` REMOVED (dead after
  the fusion — its only 2 callers, both in trajectory_screen.py, now
  build the fused trajectory instead). `DETACH_LIFT_ABOVE_TARA_CM`/
  `DETACH_X_SHIFT_CM` constants kept (still Luis's fixed 1.0cm margins,
  now read directly by `TrajectoryScreen._send_and_check()`).
- `TrajectoryScreen._send_and_check()` gained an optional `detach_tara`
  param: when given, builds the fused trajectory via
  `generate_detach_and_ensayo_trajectory()` (passing
  `time_scale_spinbox.value()`) BEFORE the existing
  `validate_trajectory()`/`send_trajectory(timed=True)` calls — same
  single choke point as before, now also covering the hop. Both
  `_on_run_clicked`'s `do_run_with_detach()` and `_restart_trial`'s
  `do_restart()` updated to call `_send_and_check(detach_tara=
  tara_record.tara)` instead of the old separate
  `perform_pre_run_detach()` + resend sequence; the recorded "prueba"
  Y is now the tara's own Y (where the fused run starts from) rather
  than a post-move GET_POSITION readout, since there's no longer an
  intermediate blocking step to measure between. `do_run_with_detach`'s
  `min_duration_ms=3000` padding was removed (it existed only to keep
  the old "Despegando..." label readable during the old blocking hop,
  which no longer exists as a separate step); `do_restart`'s
  `min_duration_ms=3000` on `safe_return_to_position()`'s own label is
  UNCHANGED — that's a separate, still-real blocking move, out of
  scope for this fix.
- Side effect Luis should know about, not yet asked about: the live
  trajectory plot will now show the hop's own small lift/shift motion
  at the very start of a Run/Reiniciar Ensayo with a tara on record
  (TRAJ_PROGRESS has no way to distinguish "hop" from "ensayo" once
  they're one trajectory) — previously the hop was invisible to the
  plot (untimed, separate, plot session began only after it). Not
  filtered out; flag if it reads confusingly on the real plot.
- Verified OFFSCREEN only: fused-trajectory geometry (starts at
  `current`, hop returns to `current` before the ensayo continues,
  ensayo's own t=0 boundary de-duped, strictly increasing t, ends
  exactly on the ensayo's own last point); `time_scale` scaling
  linearly (30x hop takes ~30x longer than 1x); and — the specific
  safety check this change interacts with — confirmed a low time_scale
  (1x) now correctly gets REJECTED by the existing
  `TrajectorySpeedExceededError` gate (the hop's placeholder Y speed,
  5cm/s, exceeds the real `MAX_SPEED_Y_CM_S`=3.9cm/s ceiling at 1x)
  while the real default (30x) is correctly ACCEPTED — this is
  precisely why sharing `time_scale` with the ensayo matters, not just
  a cosmetic choice. NOT tested on real hardware: whether the fused
  hop+ensayo actually FEELS continuous on the rig, and whether the
  plot side effect above is acceptable as-is.
- Note for next session: `main.py` was observed running (PID from a
  live process, started 16:12) DURING this same session, already on
  the pre-fusion code — it will not pick up this change until
  restarted.

**Implemented (2026-09-18, later same session): angle auto-alignment
to the loaded ensayo's own recorded starting angle.** Luis's explicit
request: he shouldn't have to manually type a matching angle at "Ir a
Posición Inicial" — the operator's typed value was what actually
determined the ensayo's real starting angle (see `_offset_points()`'s
existing OFFSET design, 2026-07-25), with the CSV's own recorded first
angle only ever used as a relative reference, never reached for real
unless it happened to match by luck. Luis's explicit choice on timing:
correct for ANY real difference (with a negligible-move tolerance),
not just large ones, and the correction happens "al cargar la
trayectoria" (Load && Send time), not before.
- New `trajectory_generator.generate_angle_alignment_trajectory(current,
  target_angle, time_scale, calibration_space)` — a single angle-only
  synchronized leg (X/Y untouched), empty (no-op) if already within
  `_interpolate`'s existing negligible-move threshold. Same
  `time_scale` reasoning as `generate_detach_and_ensayo_trajectory`
  (2026-09-18, earlier this session) — kept consistent so the leg
  moves at a coherent speed and is checked against
  `MAX_SPEED_ANGLE_DEG_S` once sent TIMED.
- New `trajectory_generator.stitch_trajectories(phases)` — a small
  public wrapper around the `_append_phase` stitching every multi-leg
  generator in this module already used internally, so
  `TrajectoryScreen._send_and_check()` can compose the (independently
  generated) alignment leg ahead of whatever it already builds
  (plain offset ensayo, or the fused detach+ensayo) without reaching
  into private internals.
- `TrajectoryScreen._offset_points()` gained an optional `position`
  override param (default: `self._position_session.position`,
  unchanged behavior for every existing call site) — needed because at
  the moment offsetting runs, the alignment leg hasn't physically
  happened yet (it's part of the SAME trajectory being built), so the
  offset math needs to already ASSUME the ensayo's own target angle
  rather than the stale value `_position_session.position.angle` still
  holds.
- `_send_and_check()` (the single choke point for Load && Send,
  Reiniciar Ensayo, AND Run-with-detach — see 2026-09-18's earlier
  fusion entry) now builds this alignment leg UNCONDITIONALLY (not
  gated by a tara), before everything else: real current position ->
  ensayo's own first-row angle. When a detach hop is ALSO being fused
  (`detach_tara` given), its own `current` is taken from where the
  alignment leg ENDS, not the platform's stale real position — so the
  hop detaches/reapproaches at the ALREADY-CORRECTED angle, not the
  old one. Final order when everything is active: align -> detach hop
  -> ensayo, all ONE trajectory, ONE TRAJ_BEGIN, ONE RUN — same "no
  extra transfer, no extra gap" property as the detach fusion itself.
- NOT touched: `_check_ensayo_within_range()` (the UI-thread preview
  check at Load && Send time, before the background worker even
  starts) still previews using the OLD plain offset, same
  pre-existing gap as the detach hop already had — its own docstring
  already documents it as presentation-only, `_send_and_check()` is
  the real gate and IS alignment-aware. Not fixed; flagged as a known
  minor inconsistency, not asked to fix it this session.
- Verified OFFSCREEN only (pure-function level, mirroring exactly what
  `_send_and_check()` now does): alignment leg geometry (X/Y
  untouched, ends at target angle, no-op when already matching, rejects
  out-of-range); `stitch_trajectories` continuity/de-dup/empty-phase
  handling; a full scenario with a stale operator angle (5°) vs. a
  real CSV's first-row angle (-20°) confirming the offset ensayo's
  angle values pass through UNCHANGED (raw CSV angle, not
  re-offset) once the alignment leg is accounted for; and that the
  detach hop's own `current` correctly picks up the ALIGNED angle
  (not the stale one) when both features are active together. NOT
  tested on real hardware — same `main.py`-not-yet-restarted caveat as
  the detach fusion above.

**Implemented (2026-09-18, later still): two follow-up fixes to the
same-day detach fusion + new variability matrix, both from Luis
testing/thinking through the design further.**
1. **Detach hop now moves at "fastest safe" speed instead of the
   ensayo's own time_scale.** Luis's earlier explicit requirement
   (same day, "same scale as the CSV load") made a 1cm hop feel
   "bastante lento" once stretched by the default 30x — he asked for
   it to stay continuous/fused but move as fast as the rig safely
   allows, bounded by the real speed limits, not unbounded.
   `generate_detach_and_ensayo_trajectory()`'s `time_scale` param
   REPLACED by `max_speed_x_cm_s`/`max_speed_y_cm_s` — new
   `_fastest_leg()` (mirrors `_interpolate()`'s shape but driven by
   explicit per-axis ceilings, no time_scale multiplication) computes
   each leg's duration as `distance / (max_speed * 0.9)`
   (`_DETACH_HOP_SPEED_SAFETY_MARGIN = 0.9`, headroom against
   step-rounding pushing the ACTUAL speed a hair over the nominal
   ceiling). `TrajectoryScreen._send_and_check()` now passes
   `sm.MAX_SPEED_X_CM_S`/`sm.MAX_SPEED_Y_CM_S` instead of
   `time_scale_spinbox.value()`. The ensayo's own points (and the
   angle-alignment leg) are UNCHANGED — still at `time_scale`/fastest-
   safe respectively as before; only the detach hop's own timing
   changed. Verified offscreen: computed hop duration matches the
   fastest-safe formula exactly, is ~20x+ faster than the old 30x-
   scaled duration for the same 1cm move, and still passes the real
   `TrajectorySpeedExceededError` ceiling check (the 0.9 margin holds).
2. **"Ejecutar punto" (variability matrix) now reaches the tara via
   the SAME safe repositioning sequence as "Reiniciar Ensayo"/"Ir a
   Posición Inicial"**, instead of a direct diagonal move — Luis's
   explicit request: a matrix point must be gated by the same physical
   safety rules as every other repositioning (lift clear by
   `Y_LIFT_MARGIN_CM`=5cm before X travels, only move X at
   `ANGLE_REFERENCE_DEG`), not skip them for being a short
   calibration-style move.
   `generate_variability_point_trajectory()` gained
   `lift_margin_cm`/`angle_reference_deg` params — its own leg 1
   (current -> tara) now delegates to `generate_safe_return_trajectory`
   (`floor_y = tara.y`) instead of a plain synchronized leg; leg 2 (the
   actual depth descent, tara -> tara+depth) is unchanged, plain,
   Y-only. `SystemStateMachine.perform_variability_point()` now passes
   `self.Y_LIFT_MARGIN_CM`/`self.ANGLE_REFERENCE_DEG` through. Side
   effect (by design, matches the function's own pre-existing
   "always approach from the same reference" repeatability
   philosophy): the lift+descend now runs on EVERY sample, even when
   already sitting at tara with depth=0, so every repeat takes the
   exact same physical path. Verified offscreen: Y never drops below
   `tara.y` while X is still in transit (mirroring
   `generate_safe_return_trajectory`'s own guarantee), the intentional
   below-tara descent only happens once already at `tara.x`, and the
   lift+descend still runs even in the degenerate
   current==tara/depth==0 case.
Neither verified on real hardware — same `main.py`-not-yet-restarted
caveat as everything else built this session.

**Implemented (2026-09-18, later still): CSV initial-angle read-out on
Monitor's right column.** Luis's explicit request: purely visual, "para
confirmar, nada más" — no interaction. New
`TrajectoryScreen.csv_angle_label` in the TRAJECTORY SELECTION box
(right sidebar), between the Refresh/Load && Send row and the existing
`speed_label` — shows "Ángulo inicial CSV: —" until a trajectory is
loaded, then the loaded ensayo's own raw first-row angle (same value
`_send_and_check`'s angle-alignment leg targets), reset to "—" on a
failed/invalid load. Verified offscreen: text updates correctly on
load/reset (checked directly, without needing the background worker to
finish); a full MainWindow screenshot (welcome screen dismissed,
navigated to Monitor for real) confirms the label renders in place
with no layout overlap.

**Implemented (2026-09-18, later still): corrected WHEN a variability-
matrix sample counts, plus a per-sample delete button.** Luis's
explicit correction: "Ejecutar punto" only moves the platform to the
(talla, depth) point — he still has to press Run separately (via
TrajectoryScreen, on the already-loaded ensayo for that talla) to
actually execute the trial from there. The repeatability counter had
been incrementing on the MOVE; it must increment when that SEPARATE
Run's own trajectory finishes instead.
- `SystemStateMachine.perform_variability_point()` REPLACED by
  `go_to_variability_point(tara, depth_mm) -> Position` — now a plain
  GOTO-style blocking call (reuses `_run_trajectory_blocking`, same as
  `safe_return_to_position()`), no more device-error/pause polling or
  `VariabilityPointResult` (both removed) — it has no opinion on
  success/failure anymore, only gets the platform there.
- `VariabilityMatrixScreen` now tracks a `_pending_sample` (dict:
  trajectory_id/depth_mm/y_real/interrupted) set right after a
  successful "Ejecutar punto". Subscribes to the shared bridge's
  `trajectory_finished` (records the pending sample as success, unless
  a pause was observed first), `device_error` (records it as a
  failure), `state_changed` (marks `interrupted=True` on PAUSED; if it
  then sees IDLE while already interrupted — a pause-then-abort, which
  never fires `trajectory_finished`/`device_error` at all — finalizes
  as failed instead of leaving `_pending_sample` stuck forever), and
  `disconnected` (drops it silently, no record — nothing meaningful
  was measured). Known limitation, same as ConnectionScreen's own
  `_awaiting_initial_move` guard: trusts that the NEXT trajectory to
  finish while a sample is pending is the intended Run; an unrelated
  one finishing first would be wrongly attributed. Not solved further
  — matches an already-accepted risk elsewhere in this app.
- New `variability_library.delete_sample(trajectory_id, depth_mm,
  index)` — removes one sample from a row without touching the rest.
  New per-row "✕" button (5th column, `_SAMPLE_COLUMNS`) in the
  "PUNTO SELECCIONADO" samples table (Luis's explicit request, "un
  botón... para poder eliminar ensayo" — interpreted as deleting one
  mistaken/bad SAMPLE, not a CSV file) — `_on_delete_sample_clicked`.
- Verified offscreen (mocked controller, simulating the repositioning
  move's own FINISHED via a background thread, then the SEPARATE
  ensayo Run's FINISHED/ERROR/pause-abort via direct calls into the
  state machine): "Ejecutar punto" alone records nothing; the
  subsequent Run's FINISHED is what records exactly 1 sample; an
  unrelated FINISHED with nothing pending is a safe no-op; a device
  error during the Run records a failure; pause-then-abort records a
  failure instead of hanging; a disconnect drops the pending sample
  with no record; `delete_sample` removes the right one, rejects an
  out-of-range index/invalid depth row, and the UI handler wires
  through correctly. Screenshot confirms the "✕" buttons render
  cleanly per row. NOT tested on real hardware.

**Implemented (2026-09-21): 5cm lift-off at the END of every ensayo
(Run and Reiniciar Ensayo).** Luis's explicit request: when the
trajectory finishes, the platform must rise 5cm immediately ("sin
delay") to detach from the ground/force platform.
- Same fusion technique as the 2026-09-18 detach hop: new
  `trajectory_generator.append_end_lift()` appends a pure-Y leg onto the
  END of the same timed trajectory (ONE TRAJ_BEGIN, ONE RUN), so the
  ESP32 goes straight from the ensayo's last point into the lift — no
  FINISHED-then-second-transfer gap. Fastest-safe timing
  (`_fastest_leg`, same 0.9 margin as the hop), NOT stretched by
  `time_scale`; capped at `calibration_space.y_max` (no-op if already
  there). `SystemStateMachine.END_LIFT_CM = 5.0` (fixed, not UI-
  configurable, same as the detach constants).
- Applied in `TrajectoryScreen._send_and_check()` unconditionally (not
  gated by a tara), so Load && Send, plain Run, Run-with-detach and
  every leg of a Reiniciar Ensayo repeat sequence all get it. No wire/
  firmware change (`docs/protocol.md` untouched).
- Side effects: `FINISHED` (and the matrix's sample recording / repeat-
  sequence chaining) now arrives ~1.4s after the last CSV point, once
  the lift completes; the live plot shows the lift as a Y rise at the
  end (TRAJ_PROGRESS can't tell it apart from the ensayo). Rig ends 5cm
  above the last ensayo point; `_position_session.position` still holds
  the INITIAL position (unchanged), and later moves use real
  `GET_POSITION`, so nothing depends on where the rig ended.
- Verified OFFSCREEN only: generator geometry (+5cm Y, X/angle
  unchanged, ensayo points untouched, y_max cap, empty input); the real
  `_send_and_check()` (plain and with-tara) against a real
  `SystemStateMachine` speed-ceiling gate with the CSV at 30x. NOT
  tested on real hardware; `main.py` must be restarted to pick it up.

## Deferred / not built yet (do not build unless explicitly asked)
GUI polish (splash screen, branding), user management, pathology
library, automatic reports, Digital Twin, sensor integration beyond
current scope — all explicitly out of thesis scope per prior planning.
## Roadmap (two horizons — for context, not a license to skip confirmation)

**Horizon 1 — test-firmware phase, DONE as of 2026-07-31** (see status
block above): RPi control app against the simulated test firmware —
connection, homing, manual movement, trajectory select/send/run, and
live plotting (pos_x, pos_y, angle) during RUNNING — verified on real
hardware (real Raspberry Pi + real test ESP32), not just code-reviewed.

**Horizon 2 — CURRENT as of 2026-08-31 (pivot), but PAUSED on firmware-
side work specifically:** Luis moved testing to the teammate's real/
definitive firmware instead of the test firmware (see "PIVOT" in the
status block above). A copy of that firmware
(`firmware/platformIO_control_trayectoria/`) is in the repo but is
explicitly known-STALE — do NOT treat it as reference, do NOT start
the Hard-rule-6 comment-annotation review/integration pass, until Luis
says he has an updated copy. Until then, verify/build only against
`docs/protocol.md` + the RPi-side code, same as before.

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