# Gait Simulator — Serial Communication Protocol

This document specifies the contract between the Raspberry Pi
(high-level controller) and the ESP32 (real-time controller). Any
firmware implementation (test or production) must comply with this
specification for the rest of the software stack to work unmodified.

## Transport

- Physical link: USB Serial
- Baud rate: 115200
- Roles: Raspberry Pi is master (sends commands), ESP32 is slave
  (executes and responds)
- **RPi -> ESP32 commands**: framed with literal `<` and `>` delimiters,
  no trailing `\n` — e.g. `PING` is sent on the wire as `<PING>`. The
  ESP32 reads from `<` to `>` as one complete command (matching the
  general-purpose receive function used on the ESP32 side). Every
  `Format` cell below for an RPi -> ESP32 command shows only the
  payload; wrap it in `<` `>` to get the actual bytes sent.
- **ESP32 -> RPi responses**: unaffected by the above — plain ASCII
  text, one response per line, terminated with `\n`, exactly as shown
  in the `Response`/`Format` cells below.
- **Argument types (RPi -> ESP32 commands with 2+ arguments)**: the
  first argument may be a string (e.g. `axis`); every argument after
  the first must be numeric — either an integer, or (as with
  `TRAJ_POINT`/`GOTO`, where every argument is already numeric) a
  float. No command may carry a second textual/categorical argument —
  e.g. `MANUAL`/`MOVE_REL`'s `direction` is an integer (`1`/`0`), not
  `+`/`-` text, precisely so it isn't a second string argument. This
  matches the ESP32-side general-purpose receive function, which parses
  argument 0 as a string and every argument after it as numeric.

## System Commands

| Command | Direction | Format | Response |
|---|---|---|---|
| Ping | RPi -> ESP32 | `<PING>` | `PONG` |
| Home | RPi -> ESP32 | `<HOME>` | `READY` on success, `ERROR:<code>:<msg>` on failure |
| Status query | RPi -> ESP32 | `<STATUS>` | `STATUS:<state>` |

## Calibration Events (unsolicited, during HOMING)

`<HOME>` triggers homing AND a full limit-mapping sweep of the 3 axes,
in a fixed order: **Y, then X, then Angular**. While `HOMING`, before
the final `READY`, the ESP32 emits a sequence of unsolicited events per
axis so the Raspberry Pi can compute the available range of movement —
same spirit as `TRAJ_PROGRESS` during `RUNNING`.

**Framing exception**: unlike every other ESP32 -> RPi response (which
is unframed plain text, see Transport), these calibration events ARE
wrapped in `<` `>`, and `CAL_PROGRESS`'s two arguments follow the same
string-first/numeric-rest, colon-separated shape as multi-argument
RPi -> ESP32 commands like `MANUAL`/`MOVE_REL` — by explicit request,
so this argument convention is applied uniformly regardless of
direction. This deliberately does NOT apply retroactively to existing
responses (`READY`, `TRAJ_PROGRESS`, `POSITION`, `STATUS`, `ACK`,
`ERROR`, etc.) — those remain unframed as documented under Transport.

| Response | Format | Notes |
|---|---|---|
| Min limit reached | `<LIM{AXIS}MIN>` | No arguments. Defines that axis's raw zero. One of `<LIMYMIN>`, `<LIMXMIN>`, `<LIMANGMIN>`. |
| Travel progress | `<CAL_PROGRESS:axis:value>` | `axis` (string, first argument) one of `Y`/`X`/`A`. `value` (numeric, second argument) is the distance covered so far from that axis's min, in real units (cm for Y/X, degrees for `A`) — same unit convention as `GET_POSITION`/`GOTO`, never raw motor steps (see Absolute Positioning Commands). Sent repeatedly while traveling toward the max limit. |
| Max limit reached | `<LIM{AXIS}MAX:value>` | Single numeric argument: the final measured range for that axis (same units as `CAL_PROGRESS`) — authoritative regardless of whether every intermediate `CAL_PROGRESS` tick was received. One of `<LIMYMAX:value>`, `<LIMXMAX:value>`, `<LIMANGMAX:value>`. |

All 3 axes are reported RAW/limit-relative on the wire — `[0, value]`.
Sequence for a full `HOME`:

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

The Raspberry Pi combines the 3 per-axis raw ranges into the full
available movement-space map. This whole sequence happens once per
`HOME` call (itself only once per session — see
`SystemStateMachine.can_home()`). Note `READY` itself, closing out the
sequence, stays unframed — it is the pre-existing `HOME` response, not
one of the new calibration events.

**Angular axis is relative to horizontal, applied on the RPi side —
NOT a wire-format concern**: `LIMANGMIN`'s raw `0` is where the
*mechanical limit switch* is touched, which is NOT the same as level/
horizontal — there's a real physical offset between the two. Rather
than complicate the wire protocol (which stays uniform across all 3
axes, as above), the Raspberry Pi applies a single fixed offset once,
when reducing the raw `[0, a_range]` sweep into the final available-
space map, so the reported angle range ends up relative to horizontal
instead of relative to the limit switch. See
`SystemStateMachine.ANGLE_HORIZONTAL_OFFSET_DEG` in `system_state.py`
for the current placeholder value (`-44.0`) and
`SystemStateMachine.CalibrationSpace` for where it's applied.

**Provisional**: like `TRAJ_PROGRESS`, the exact `CAL_PROGRESS` cadence
and per-axis simulated range are defined by the test firmware
(`firmware/gaitsim-esp32-test/main.cpp`) purely to exercise this flow
end-to-end and do not represent real homing timing or real mechanical
limits. The definitive firmware (Horizon 2) is expected to emit the
same event structure (`LIM*MIN`, `CAL_PROGRESS`, `LIM*MAX`) with real
timing and real limit-switch-derived values.

## Manual Movement Commands

Only valid when the system is NOT calibrating and NOT running a trajectory.

| Command | Format | Response |
|---|---|---|
| Manual move | `<MANUAL:axis:direction:steps>` | `OK` or `ERROR:<code>:<msg>` |
| Stop | `<STOP>` | `STOPPED` |

- `<axis>`: `X` (horizontal), `Y` (vertical), `A` (sagittal angle)
- `<direction>`: integer, `1` = `+`, `0` = `-` (see Transport note on
  argument types below)
- `<steps>`: positive integer

## Relative Movement Commands (real units)

An alternative to `MANUAL` for callers that want to move by a known
real-world amount (cm/deg) instead of raw motor steps — used by the
Raspberry Pi's manual-adjustment buttons once a reference position
exists (after `HOME` and/or `GOTO`). Only valid when the system is
IDLE, same restriction as `MANUAL`.

| Command | Format | Response |
|---|---|---|
| Move relative | `<MOVE_REL:axis:direction:amount>` | `OK` or `ERROR:<code>:<msg>` |

- `<axis>`, `<direction>`: same meaning and encoding as `MANUAL`.
- `<amount>`: positive float, in the same units as `GOTO`/`TRAJ_POINT`
  (cm for `X`/`Y`, degrees for `A`).
- Updates the same tracked position `GET_POSITION` reports. Unlike
  `MANUAL`, no steps-to-units conversion is needed since the amount is
  already given in real units.

## Absolute Positioning Commands

`GOTO` is only valid when the system is IDLE (not homing, not
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
  including the cumulative effect of any `MANUAL` moves performed
  since the last `HOME` or `GOTO`. This is the only way the Raspberry
  Pi can learn the real-unit result of a step-based manual move, since
  it does not itself know any steps-to-units conversion — that
  conversion is the firmware's responsibility, same as the physical
  calibration it already performs during `HOME`.
- The tracked position must also be updated as a trajectory executes
  (once per `TRAJ_POINT` reached, i.e. in step with each
  `TRAJ_PROGRESS`), not only by `MANUAL`/`GOTO` — after a `PAUSE` or
  `ABORT`, `GET_POSITION` is the only way the Raspberry Pi knows where
  the system actually stopped mid-trajectory, which the safe
  repositioning sequence (see `SystemStateMachine.safe_return_to_position`
  in system_state.py) depends on.

## Trajectory Transfer Commands

A trajectory is a sequence of points, each with time, horizontal
position, vertical position, and sagittal angle, all sharing the same
time base (they originate from a single CSV with columns
`time, pos_x, pos_y, angle`).

| Command | Format | Response |
|---|---|---|
| Begin transfer | `<TRAJ_BEGIN:n_points>` | `TRAJ_READY` |
| Send point | `<TRAJ_POINT:t,x,y,angle>` | `ACK:<index>` |
| End transfer | `<TRAJ_END>` | `TRAJ_STORED` or `ERROR:<code>:<msg>` |

`<n_points>` can vary between trajectories (not fixed to any specific
count, e.g. not always ~120). The ESP32 must validate that the number
of `TRAJ_POINT` messages received before `TRAJ_END` matches
`<n_points>`; a mismatch results in `ERROR:POINT_COUNT_MISMATCH:...`.

## Execution Commands

| Command | Format | Response |
|---|---|---|
| Run | `<RUN>` | `RUNNING` immediately, then one `TRAJ_PROGRESS` per point (in order) while executing, then `FINISHED` when the cycle completes |
| Pause | `<PAUSE>` | `PAUSED` |
| Resume | `<RESUME>` | `RUNNING` |
| Abort | `<ABORT>` | `ABORTED` on success, `ERROR:INVALID_STATE:...` otherwise |

`ABORT` abandons a paused trajectory entirely (operator chose to restart
the trial or run a different one instead of resuming) and returns the
system to `IDLE`, discarding remaining trajectory progress — unlike
`STOP`, which is a resumable pause (see System Flow below). Only valid
while `PAUSED`; the ESP32 must reject it otherwise with
`ERROR:INVALID_STATE:...`. The system's tracked position (`GET_POSITION`)
must reflect wherever execution actually stopped — see the note under
Absolute Positioning Commands.

### Execution progress (unsolicited, during RUNNING)

| Response | Format | Notes |
|---|---|---|
| Progress | `TRAJ_PROGRESS:<t>,<x>,<y>,<angle>` | One per stored trajectory point, sent in order as each point is "executed". Not a response to any single command — arrives asynchronously while the system is RUNNING, same as `FINISHED`. |

**Provisional**: this message and its exact pacing are defined by the test
firmware (`firmware/gaitsim-esp32-test/main.cpp`) to support live plotting on
the Raspberry Pi (see CLAUDE.md). The test firmware emits one `TRAJ_PROGRESS`
every fixed 120ms per point, regardless of the point's own `t` value —
this cadence is test-only, purely to make the live plot visible, and does
NOT represent real execution timing. The definitive firmware (Horizon 2) will
have its own real timing between points (unknown for now), but is expected to
emit the same per-point structure (`t,x,y,angle`) so the Raspberry Pi side
does not need to change when that firmware becomes available.

## Error Format

All errors follow the same structure, regardless of which command
triggered them:


ERROR:<code>:<short message>

Examples:
- `ERROR:LIMIT_REACHED:X axis`
- `ERROR:INVALID_STATE:cannot home while running`
- `ERROR:POINT_COUNT_MISMATCH:expected 120, got 118`

Error codes are not yet formally enumerated; as new error conditions
are identified during firmware development, they should be documented
here.

## System Flow (reference)

1. RPi sends `<PING>` to verify the link.
2. RPi sends `<HOME>`. ESP32 performs homing and limit mapping, resets
   its tracked position to `(0, 0, 0)`, then responds `READY`. This
   happens once per power-on session.
3. Optionally, RPi sends `<GOTO:x,y,angle>` to move to a starting
   position before sending/running a trajectory (only while IDLE). The
   position can be fine-tuned afterwards with `MANUAL` moves and its
   real-unit result read back with `GET_POSITION`.
4. For each trial:
   - RPi sends `<TRAJ_BEGIN:n_points>`
   - RPi sends `<TRAJ_POINT:...>` for each point
   - RPi sends `<TRAJ_END>`
   - RPi sends `<RUN>`
   - ESP32 responds `RUNNING`, then `FINISHED` when done
   - System is ready for the next trajectory without re-homing
5. Manual movement (`<MANUAL:...>`) is only accepted when the system is
   idle (not homing, not running).
6. `<STOP>` acts as an immediate pause during execution; `<RESUME>`
   continues from where it left off.

## Design Notes

- Plain text was chosen over binary for readability during
  development/debugging and because trajectory payloads are small
  (tens to low hundreds of points per trial).
- Consolidating `x`, `y`, `angle` into a single `TRAJ_POINT` message
  (instead of three separate per-axis transfers) was a deliberate
  choice: all three axes share the same time base within one gait
  cycle, so sending them together avoids the need for the firmware to
  synchronize three independent arrays.
