# Gait Simulator — Serial Communication Protocol

This document specifies the contract between the Raspberry Pi
(high-level controller) and the ESP32 (real-time controller). Any
firmware implementation (test or definitive) must comply with this
specification for the rest of the software stack to work without
modification.

## Transport

- Physical link: USB Serial
- Baud rate: 115200
- Roles: the Raspberry Pi is master (sends commands), the ESP32 is
  slave (executes and responds)
- **RPi -> ESP32 commands**: framed with the literal delimiters `<`
  and `>`, with no trailing `\n` — e.g. `PING` is sent over the wire
  as `<PING>`. The ESP32 reads from `<` to `>` as a complete command
  (matches the generic receive function used on the ESP32 side). Each
  `Format` cell below for an RPi -> ESP32 command shows only the
  payload; it must be wrapped in `<` `>` to get the actual bytes sent.
- **ESP32 -> RPi responses**: not affected by the above — plain ASCII
  text, one response per line, terminated with `\n`, exactly as shown
  in the `Response`/`Format` cells below.
- **Argument types (RPi -> ESP32 commands with 2+ arguments)**: the
  first argument may be a string (e.g. `axis`); every argument after
  the first must be numeric — integer, or (as in
  `TRAJ_POINT`/`GOTO`, where all arguments are already numeric) a
  float. No command may carry a second textual/categorical argument —
  e.g. `MANUAL`/`MOVE_REL`'s `direction` is an integer (`1`/`0`), not
  `+`/`-` text, precisely so it isn't a second string-type argument.
  This matches the generic receive function on the ESP32 side, which
  parses argument 0 as a string and every subsequent argument as
  numeric.

## System Commands

| Command | Direction | Format | Response |
|---|---|---|---|
| Ping | RPi -> ESP32 | `<PING>` | `PONG` |
| Home | RPi -> ESP32 | `<HOME>` | `READY` on success, `ERROR:<code>:<msg>` on failure |
| Status query | RPi -> ESP32 | `<STATUS>` | `STATUS:<state>` |

`<state>` must be one of the following 6 values — the same states
used by the Raspberry Pi's `SystemStateMachine` (see the "State
machine states" section of CLAUDE.md), so the firmware must maintain
an internal enum that mirrors this and simply report whichever value
is currently active (no extra logic needed in the handler — see
`handleStatus()` in the test firmware at
`firmware/gaitsim-esp32-test/main.cpp` as a reference):

| `<state>` | When the ESP32 must be in this state |
|---|---|
| `DISCONNECTED` | Initial state on boot, before `HOME` has succeeded at least once this session |
| `HOMING` | While running the calibration sweep, between receiving `<HOME>` and sending `READY` |
| `IDLE` | Homed, not running a trajectory, not receiving a trajectory — ready for `MANUAL`/`GOTO`/`TRAJ_BEGIN` |
| `RECEIVING_TRAJECTORY` | Between `<TRAJ_BEGIN>` and `<TRAJ_END>` |
| `RUNNING` | After `<RUN>`, before `FINISHED`/`PAUSED`/`ABORTED` |
| `PAUSED` | After `<PAUSE>` or `<STOP>`, before `<RESUME>` or `<ABORT>` |

## Calibration Events (unsolicited, during HOMING)

`<HOME>` triggers homing AND a full limit-mapping sweep of all 3 axes,
in a fixed order: **Y, then X, then Angular**. While in `HOMING`,
before the final `READY`, the ESP32 emits a sequence of unsolicited
events per axis so the Raspberry Pi can compute the available range of
motion — same spirit as `TRAJ_PROGRESS` during `RUNNING`.

**Framing exception**: unlike any other ESP32 -> RPi response (which
is plain unframed text, see Transport), these calibration events ARE
wrapped in `<` `>`, and `CAL_PROGRESS`'s two arguments follow the same
string-first/numeric-rest shape, colon-separated, used by multi-argument
RPi -> ESP32 commands like `MANUAL`/`MOVE_REL` — by explicit request,
so this argument convention applies uniformly regardless of direction.
This is deliberately NOT applied retroactively to existing responses
(`READY`, `TRAJ_PROGRESS`, `POSITION`, `STATUS`, `ACK`, `ERROR`, etc.)
— those remain unframed, as documented in Transport.

| Response | Format | Notes |
|---|---|---|
| Minimum limit reached | `<LIM{AXIS}MIN>` | No arguments. Defines that axis's raw zero. One of `<LIMYMIN>`, `<LIMXMIN>`, `<LIMANGMIN>`. |
| Travel progress | `<CAL_PROGRESS:axis:value>` | `axis` (string, first argument) one of `Y`/`X`/`A`. `value` (numeric, second argument) is the distance traveled so far from that axis's minimum, in real units (cm for Y/X, degrees for `A`) — same unit convention as `GET_POSITION`/`GOTO`, never raw motor steps (see Absolute Positioning Commands). Sent repeatedly while traveling toward the maximum limit. |
| Maximum limit reached | `<LIM{AXIS}MAX:value>` | A single numeric argument: the final measured range for that axis (same units as `CAL_PROGRESS`) — authoritative regardless of whether every intermediate `CAL_PROGRESS` was received. One of `<LIMYMAX:value>`, `<LIMXMAX:value>`, `<LIMANGMAX:value>`. |

All 3 axes are reported raw/relative-to-limit on the wire —
`[0, value]`. Sequence for a complete `HOME`:

```
<LIMYMIN>
<CAL_PROGRESS:Y:...>   (repeated)
<LIMYMAX:y_range>
<LIMXMIN>
<CAL_PROGRESS:X:...>   (repeated)
<LIMXMAX:x_range>
<LIMANGMIN>
<CAL_PROGRESS:A:...>   (repeated)
<LIMANGMAX:a_range>
READY
```

The Raspberry Pi combines the 3 raw per-axis ranges into the complete
map of the available motion space. This whole sequence happens once
per `HOME` call (which itself only happens once per session — see
`SystemStateMachine.can_home()`). Note that `READY` itself, which
closes the sequence, remains unframed — it is `HOME`'s pre-existing
response, not one of the new calibration events.

**The angular axis is relative to horizontal, applied on the RPi
side — NOT a wire-format concern**: `LIMANGMIN`'s raw `0` is where the
*mechanical limit switch* is touched, which is NOT the same as
level/horizontal — there is a real physical offset between the two.
Rather than complicating the wire protocol (which stays uniform
across the 3 axes, as above), the Raspberry Pi applies a single fixed
offset when reducing the raw `[0, a_range]` sweep to the final
available-space map, so that the reported angle range ends up relative
to horizontal instead of relative to the limit switch. See
`SystemStateMachine.ANGLE_HORIZONTAL_OFFSET_DEG` in `system_state.py`
for the current placeholder value (`-44.0`) and
`SystemStateMachine.CalibrationSpace` for where it's applied.

**Provisional**: like `TRAJ_PROGRESS`, `CAL_PROGRESS`'s exact cadence
and the simulated range per axis are defined by the test firmware
(`firmware/gaitsim-esp32-test/main.cpp`) purely to exercise this
flow end-to-end, and do not represent real homing timing or real
mechanical limits. The definitive firmware (Horizon 2) is expected to
emit the same event structure (`LIM*MIN`, `CAL_PROGRESS`, `LIM*MAX`)
with real timing and values derived from the actual limit switches.

## Manual Movement Commands

Only valid when the system is NOT calibrating and NOT running a
trajectory.

| Command | Format | Response |
|---|---|---|
| Manual movement | `<MANUAL:axis:direction:steps>` | `OK` or `ERROR:<code>:<msg>` |
| Stop | `<STOP>` | `STOPPED` |

- `<axis>`: `X` (horizontal), `Y` (vertical), `A` (sagittal angle)
- `<direction>`: integer, `1` = `+`, `0` = `-` (see the Transport note
  on argument types above)
- `<steps>`: positive integer

## Relative Movement Commands (real units)

An alternative to `MANUAL` for moving by a known real-world amount
(cm/degrees) instead of raw motor steps — used by the Raspberry Pi's
manual adjustment buttons once a reference position exists (after
`HOME` and/or `GOTO`). Only valid while the system is IDLE, same
restriction as `MANUAL`.

| Command | Format | Response |
|---|---|---|
| Relative movement | `<MOVE_REL:axis:direction:amount>` | `OK` or `ERROR:<code>:<msg>` |

- `<axis>`, `<direction>`: same meaning and encoding as `MANUAL`.
- `<amount>`: positive float, in the same units as `GOTO`/`TRAJ_POINT`
  (cm for `X`/`Y`, degrees for `A`).
- Updates the same tracked position reported by `GET_POSITION`. Unlike
  `MANUAL`, no step-to-unit conversion is needed since the amount is
  given directly in real units.

## Absolute Positioning Commands

`GOTO` is only valid while the system is IDLE (not homing, not
receiving a trajectory, not running) — same restriction as Manual
Movement. `GET_POSITION` is a read-only query, allowed in any state
(like `STATUS`).

| Command | Format | Response |
|---|---|---|
| Go to position | `<GOTO:x,y,angle>` | `OK` or `ERROR:<code>:<msg>` |
| Get position | `<GET_POSITION>` | `POSITION:<x>,<y>,<angle>` |

- `<x>`, `<y>`, `<angle>`: same units and reference frame as
  `TRAJ_POINT` below — `x`/`y` in cm, `angle` in degrees, relative to
  the `(0, 0, 0)` reference established by the most recent successful
  `HOME`.
- `GOTO` moves directly to an absolute position, unlike `MANUAL`'s
  relative, step-based movement.
- `GET_POSITION` reports the system's current tracked position,
  including the cumulative effect of any `MANUAL` movement made since
  the last `HOME` or `GOTO`. This is the only way the Raspberry Pi can
  learn the real-unit result of a step-based manual move, since it
  has no step-to-unit conversion of its own — that conversion is the
  firmware's responsibility, just like the physical calibration it
  already performs during `HOME`.
- The tracked position must also be updated as a trajectory executes
  (once per `TRAJ_POINT` reached, i.e. in sync with each
  `TRAJ_PROGRESS`), not only by `MANUAL`/`GOTO` — after a `PAUSE` or
  `ABORT`, `GET_POSITION` is the only way the Raspberry Pi knows where
  the system actually stopped mid-trajectory, which the safe
  repositioning sequence depends on (see
  `SystemStateMachine.safe_return_to_position` in system_state.py).

## Trajectory Transfer Commands

A trajectory is a sequence of points, each with time, horizontal
position, vertical position, and sagittal angle, all sharing the same
time base (originating from a single CSV with columns `time, pos_x,
pos_y, angle`).

| Command | Format | Response |
|---|---|---|
| Start transfer | `<TRAJ_BEGIN:n_points>` | `TRAJ_READY` |
| Send point | `<TRAJ_POINT:t,x,y,angle>` | `ACK:<index>` |
| End transfer | `<TRAJ_END>` | `TRAJ_STORED` or `ERROR:<code>:<msg>` |

`<n_points>` can vary between trajectories (not fixed to any specific
count, e.g. not always ~120). The ESP32 must validate that the number
of `TRAJ_POINT` messages received before `TRAJ_END` matches
`<n_points>`; a mismatch results in `ERROR:POINT_COUNT_MISMATCH:...`.

## Execution Commands

| Command | Format | Response |
|---|---|---|
| Run | `<RUN>` | `RUNNING` immediately, then one `TRAJ_PROGRESS` per point (in order) while executing, then `FINISHED` when the cycle ends |
| Pause | `<PAUSE>` | `PAUSED` |
| Resume | `<RESUME>` | `RUNNING` |
| Abort | `<ABORT>` | `ABORTED` on success, `ERROR:INVALID_STATE:...` otherwise |

`ABORT` fully abandons a paused trajectory (the operator chose to
restart the trial or run a different one instead of resuming) and
returns the system to `IDLE`, discarding the remaining trajectory
progress — unlike `STOP`, which is a resumable pause (see System Flow
below). Only valid while `PAUSED`; the ESP32 must reject it in any
other case with `ERROR:INVALID_STATE:...`. The system's tracked
position (`GET_POSITION`) must reflect where execution actually
stopped — see the note under Absolute Positioning Commands.

### Execution Progress (unsolicited, during RUNNING)

| Response | Format | Notes |
|---|---|---|
| Progress | `TRAJ_PROGRESS:<t>,<x>,<y>,<angle>` | One per stored trajectory point, sent in order as each point is "executed". Not a response to any particular command — arrives asynchronously while the system is RUNNING, same as `FINISHED`. |

**Provisional**: this message and its exact cadence are defined by
the test firmware (`firmware/gaitsim-esp32-test/main.cpp`) to support
live plotting on the Raspberry Pi (see CLAUDE.md). The test firmware
emits a `TRAJ_PROGRESS` every fixed 120ms per point, regardless of
the point's own `t` value — this cadence is test-only, purely to make
the live plot visible, and does NOT represent real execution timing.
The definitive firmware (Horizon 2) will have its own real timing
between points (unknown for now), but is expected to emit the same
per-point structure (`t,x,y,angle`) so the Raspberry Pi side doesn't
need to change once that firmware is available.

## Error Format

All errors follow the same structure, regardless of which command
caused them:


ERROR:<code>:<short message>

Examples:
- `ERROR:LIMIT_REACHED:X axis`
- `ERROR:INVALID_STATE:cannot home while running`
- `ERROR:POINT_COUNT_MISMATCH:expected 120, got 118`

Error codes are not yet formally enumerated; as new error conditions
are identified during firmware development, they must be documented
here.

## System Flow (reference)

1. The RPi sends `<PING>` to verify the link.
2. The RPi sends `<HOME>`. The ESP32 performs homing and limit
   mapping, resets its tracked position to `(0, 0, 0)`, and then
   responds `READY`. This happens once per power-on session.
3. Optionally, the RPi sends `<GOTO:x,y,angle>` to move to an initial
   position before sending/running a trajectory (only while IDLE).
   The position can be fine-tuned afterward with `MANUAL` movements,
   and its real-unit result can be read back with `GET_POSITION`.
4. For each trial:
   - The RPi sends `<TRAJ_BEGIN:n_points>`
   - The RPi sends `<TRAJ_POINT:...>` for each point
   - The RPi sends `<TRAJ_END>`
   - The RPi sends `<RUN>`
   - The ESP32 responds `RUNNING`, then `FINISHED` when done
   - The system is ready for the next trajectory without needing to
     re-home
5. Manual movement (`<MANUAL:...>`) is only accepted when the system
   is idle (not homing, not running).
6. `<STOP>` acts as an immediate pause during execution; `<RESUME>`
   continues from where it left off.

## Design Notes

- Plain text was chosen over binary for readability during
  development/debugging and because trajectory payloads are small
  (tens to a few hundred points per trial).
- Consolidating `x`, `y`, `angle` into a single `TRAJ_POINT` message
  (instead of three separate per-axis transfers) was a deliberate
  decision: all three axes share the same time base within a gait
  cycle, so sending them together avoids the firmware having to
  synchronize three independent arrays.
