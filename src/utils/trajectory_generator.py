"""
trajectory_generator.py

Generates a synchronized ("optimal") multi-axis trajectory between two
(x, y, angle) positions, as an alternative to an instantaneous GOTO
jump. "Synchronized" means all 3 axes (X, Y, angle) start and finish at
exactly the same time: the axis with the largest travel time (distance
/ its own max speed) sets the total duration, and the other axes are
sampled along their own straight line from their start value to their
target value over that same duration — a straight line in
configuration space, not a per-axis independent move.

Output is a plain List[TrajectoryPoint], the same type produced by
trajectory_loader.py from a CSV — the result is meant to be sent and
run through the existing TRAJ_BEGIN/TRAJ_POINT/TRAJ_END + RUN protocol
(see docs/protocol.md), which is already generic over where the points
came from. No new wire protocol or firmware changes are needed for
this.

Three entry points:
    - generate_synchronized_trajectory(target, calibration_space,
      start=...): a single synchronized straight-line move, used as-is
      for the post-calibration (0, 0, 0) -> initial-position move.
    - generate_safe_return_trajectory(...): 5 such moves stitched into
      one continuous trajectory, used for repositioning FROM an
      arbitrary current position (restart trial / choose other trial)
      without ever letting Y drop below a safety floor while X/angle
      are still in transit, AND without ever moving X off the
      reference angle — see that function's docstring and
      SystemStateMachine.safe_return_to_position(), which is the only
      caller.
    - generate_detach_and_ensayo_trajectory(...): a 2-leg "platform
      detach" hop FUSED onto the ensayo's own points into one
      continuous trajectory, used right before Run when a tara (basal,
      no-contact reference) is on record for the loaded ensayo, so a
      force-platform contact test doesn't start the run already in
      contact — see that function's docstring and
      TrajectoryScreen._send_and_check(), which is the only caller.
    - generate_variability_point_trajectory(...): 2 such moves stitched
      together (current -> tara -> a fixed depth below tara), used by
      the height-variability/repeatability test matrix — see that
      function's docstring and SystemStateMachine.go_to_variability_point(),
      which is the only caller.
    - generate_angle_alignment_trajectory(...): a single angle-only leg
      (X/Y untouched) that rotates to a target angle, used to bring the
      platform to the loaded ensayo's own recorded starting angle
      before it runs — see that function's docstring and
      TrajectoryScreen._send_and_check(), which is the only caller.

Plus stitch_trajectories(...), a small public wrapper around the
internal multi-leg stitching (_append_phase) every generator above
already uses — lets a caller compose independently-generated phases
(e.g. an angle-alignment leg ahead of a detach+ensayo trajectory)
without reaching into private internals.
"""

from typing import List

from src.communication.protocol import Position, TrajectoryPoint
from src.controllers.system_state import CalibrationSpace
from src.utils.trajectory_validator import PositionOutOfRangeError, validate_position

# Assumed max speed per axis (cm/s for X/Y, deg/s for angle), used only
# to compute how long each axis WOULD take on its own so the slowest
# axis can set the shared total duration. Placeholders until the real
# rig's motor speeds are known — same spirit as
# SystemStateMachine.Y_LIFT_MARGIN_CM, not tied to any real mechanical
# measurement yet.
SPEED_X_CM_S = 5.0
SPEED_Y_CM_S = 5.0
SPEED_ANGLE_DEG_S = 15.0

# Below this total distance (across all 3 axes combined), the move is
# treated as a no-op rather than generating a degenerate near-zero-
# duration trajectory.
_MIN_MOVE_DISTANCE = 1e-6


def _interpolate(
    start: Position, target: Position, time_scale: float = 1.0
) -> List[TrajectoryPoint]:
    """
    Core synchronized straight-line move from `start` to `target`, with
    NO calibration-space validation — callers are responsible for
    validating whatever of `start`/`target` is operator input before
    calling this (see generate_synchronized_trajectory, which validates
    `target`; generate_safe_return_trajectory, which validates `target`
    once and derives its own intermediate waypoints from already-real/
    already-validated positions).

    `time_scale`: multiplies the computed duration (default 1.0 = the
    placeholder SPEED_*_CM_S/DEG_S values unchanged, every existing
    caller's behavior). Added 2026-09-18 for
    generate_detach_and_ensayo_trajectory(), which must stretch its
    detach legs by the SAME factor TrajectoryScreen's time_scale_spinbox
    already applies to the ensayo's own recorded timing (Luis's
    explicit requirement) — otherwise a hop generated at the raw
    placeholder speed and an ensayo slowed down 30x would move at
    wildly inconsistent speeds despite being sent as one continuous
    trajectory.

    Returns exactly 2 points — (start.x, start.y, start.angle) at t=0
    and (target.x, target.y, target.angle) at t=T — rather than
    subdividing the straight line into intermediate waypoints: the
    ESP32 firmware moves all 3 axes concurrently at their own constant
    velocity (delta/T) for the whole span of a single TRAJ_POINT (see
    run_trajectory() in the definitive firmware's main.cpp), so one
    point per straight segment already produces smooth synchronized
    motion — subdividing further only adds points without changing the
    physical motion, and risks exceeding the firmware's fixed-size
    trajectory buffer (MAX_DATA_LENGTH, configurable_motion_data.h) on
    long or multi-phase moves. The leading t=0 point exists so callers
    can validate/stitch against a known start; it never reaches the
    wire as a TRAJ_POINT (SystemStateMachine._points_to_step_deltas()
    drops any point at t=0, since the ESP32 rejects a zero-duration
    TRAJ_POINT). Empty list if target is indistinguishable from start
    (nothing to move).
    """
    delta_x = target.x - start.x
    delta_y = target.y - start.y
    delta_angle = target.angle - start.angle

    if abs(delta_x) + abs(delta_y) + abs(delta_angle) < _MIN_MOVE_DISTANCE:
        return []

    duration_x = abs(delta_x) / SPEED_X_CM_S
    duration_y = abs(delta_y) / SPEED_Y_CM_S
    duration_angle = abs(delta_angle) / SPEED_ANGLE_DEG_S
    total_duration = max(duration_x, duration_y, duration_angle) * time_scale

    return [
        TrajectoryPoint(t=0.0, x=start.x, y=start.y, angle=start.angle),
        TrajectoryPoint(t=total_duration, x=target.x, y=target.y, angle=target.angle),
    ]


# Safety margin applied to MAX_SPEED_X_CM_S/MAX_SPEED_Y_CM_S when
# computing the platform-detach hop's own "fastest safe" duration (see
# _fastest_leg/generate_detach_and_ensayo_trajectory) — keeps the hop
# just under the real ceiling rather than exactly AT it: the ceiling
# check (SystemStateMachine._points_to_step_deltas) rounds each point's
# ABSOLUTE position to steps before diffing, and duration itself is
# rounded to whole milliseconds, so a duration computed from the exact
# nominal ceiling could round to an ACTUAL speed a hair over it and get
# rejected. 0.9 leaves ample headroom for that (a 1cm hop's rounding
# error is a small fraction of a step either way) while still being
# clearly faster than the ensayo's own (often 30x-stretched) pace.
_DETACH_HOP_SPEED_SAFETY_MARGIN = 0.9


def _fastest_leg(
    start: Position, target: Position,
    max_speed_x_cm_s: float, max_speed_y_cm_s: float,
) -> List[TrajectoryPoint]:
    """
    Same shape as _interpolate() (2 points, t=0/t=T, straight
    synchronized line) but driven by EXPLICIT X/Y speed ceilings
    instead of the placeholder SPEED_X_CM_S/SPEED_Y_CM_S constants —
    used only by generate_detach_and_ensayo_trajectory's hop, which
    should move as fast as the rig safely allows (Luis's explicit
    request, 2026-09-18: the hop felt "bastante lento" stretched by the
    ensayo's own time_scale, e.g. 30x, for what is really just a 1cm
    move) rather than at the ensayo's own pace. No angle component —
    the hop never rotates (see generate_detach_and_ensayo_trajectory's
    docstring), so there is no angle ceiling to pass in.
    """
    delta_x = target.x - start.x
    delta_y = target.y - start.y
    delta_angle = target.angle - start.angle

    if abs(delta_x) + abs(delta_y) + abs(delta_angle) < _MIN_MOVE_DISTANCE:
        return []

    duration_x = abs(delta_x) / max_speed_x_cm_s if max_speed_x_cm_s > 0 else 0.0
    duration_y = abs(delta_y) / max_speed_y_cm_s if max_speed_y_cm_s > 0 else 0.0
    total_duration = max(duration_x, duration_y)

    return [
        TrajectoryPoint(t=0.0, x=start.x, y=start.y, angle=start.angle),
        TrajectoryPoint(t=total_duration, x=target.x, y=target.y, angle=target.angle),
    ]


def generate_synchronized_trajectory(
    target: Position,
    calibration_space: CalibrationSpace,
    start: Position = Position(0.0, 0.0, 0.0),
) -> List[TrajectoryPoint]:
    """
    Build a synchronized straight-line trajectory from `start` to
    `target` (defaults to the HOME origin, (0, 0, 0), for the
    post-calibration initial-position move). Only `target` is
    validated against `calibration_space` — `start` is assumed to
    already be a real, reachable position (either the origin, or the
    system's actual current position as reported by GET_POSITION).

    Raises:
        PositionOutOfRangeError: If `target` falls outside the
            available movement space for any axis. No points are
            generated in that case.

    Returns:
        Exactly 2 points — t=0 (start.x, start.y, start.angle) and t=T
        (target.x, target.y, target.angle) — see _interpolate(). Empty
        list if target is indistinguishable from start (nothing to
        move).
    """
    validate_position(target, calibration_space)
    return _interpolate(start, target)


def _append_phase(
    points: List[TrajectoryPoint], phase: List[TrajectoryPoint], time_offset: float
) -> float:
    """
    Append `phase` (a trajectory starting at its own t=0) onto `points`,
    shifting its timestamps by `time_offset` so the combined trajectory
    has one continuous, strictly increasing time base. If `points`
    already ends exactly where `phase` begins (the common case when
    stitching adjacent safe-return phases), the duplicate boundary
    point is dropped rather than appended.

    Returns the time_offset the NEXT phase should use (this phase's own
    duration added on top).
    """
    if not phase:
        return time_offset
    start_index = 1 if points and phase[0].t == 0.0 else 0
    for point in phase[start_index:]:
        points.append(
            TrajectoryPoint(
                t=point.t + time_offset,
                x=point.x,
                y=point.y,
                angle=point.angle,
            )
        )
    return phase[-1].t + time_offset


def generate_safe_return_trajectory(
    current: Position,
    target: Position,
    floor_y: float,
    calibration_space: CalibrationSpace,
    lift_margin_cm: float,
    angle_reference_deg: float,
) -> List[TrajectoryPoint]:
    """
    Build a smooth repositioning trajectory from `current` (the
    system's actual current position, wherever that is) to `target` (a
    trial's initial position), WITHOUT ever letting Y drop below
    `floor_y` while X/angle are not yet at their target values, AND
    without ever moving X while angle is anywhere other than
    `angle_reference_deg` — both hard safety/mechanical rules inherited
    unchanged from the step-wise SystemStateMachine.safe_return_to_position()
    this replaces (see that method's docstring: a prosthesis may be
    mounted at `floor_y`'s height, and X travel is only mechanically
    safe at the reference angle). This is the ONLY sanctioned caller of
    this function — it exists to give that exact safety sequence
    smooth, visualized motion instead of a chain of instantaneous
    GOTOs, NOT to re-plan or shortcut it.

    Both invariants are preserved by keeping the EXACT SAME 5-step
    ordering as the original, just replacing each step's instantaneous
    jump with a synchronized (necessarily single-axis, since each step
    only ever moves one axis) sub-trajectory, stitched into one
    continuous timeline:

        1. Lift: `current` -> (current.x, lift_y, current.angle).
           Y only; X/angle unchanged.
        2. Rotate to reference: -> (current.x, lift_y, angle_reference_deg).
           Angle only, while still elevated.
        3. Move X: -> (target.x, lift_y, angle_reference_deg).
           X only, while still elevated and at the reference angle.
        4. Rotate to target: -> (target.x, lift_y, target.angle).
           Angle only, while still elevated.
        5. Descend: -> `target`. Y only; X/angle already match target,
           so this is the one step where Y may legitimately end up
           below floor_y.

    `lift_y` is computed the same way as the original step-wise
    version (`max(current.y, floor_y) + lift_margin_cm`), but capped at
    `calibration_space.y_max` — since this trajectory is fully
    generated up front (not one blocking GOTO at a time), there is no
    runtime rejection to catch and recover from mid-sequence the way
    the original could; capping proactively against the known
    calibration bounds achieves the same "don't ask for more lift than
    the rig can give" effect ahead of time instead.

    Raises:
        PositionOutOfRangeError: If `target` falls outside
            `calibration_space` on any axis. No points are generated in
            that case. `current` is NOT validated (it is the system's
            real, already-reached position).

    Returns:
        A single continuous List[TrajectoryPoint] spanning all 5
        steps, t starting at 0. Individual steps that are already
        no-ops (e.g. step 2 when current.angle already equals the
        reference) are omitted from the stitched result, but note the
        lift+descend steps still run even if `current == target`
        exactly (whenever `lift_margin_cm` > 0) — mirroring the
        original step-wise safe_return_to_position(), which never
        special-cased that either. Empty list only in the fully
        degenerate case where every step is simultaneously a no-op.
    """
    validate_position(target, calibration_space)

    lift_y = min(
        max(current.y, floor_y) + lift_margin_cm, calibration_space.y_max
    )

    step1_end = Position(current.x, lift_y, current.angle)
    step2_end = Position(current.x, lift_y, angle_reference_deg)
    step3_end = Position(target.x, lift_y, angle_reference_deg)
    step4_end = Position(target.x, lift_y, target.angle)

    points: List[TrajectoryPoint] = []
    offset = 0.0
    offset = _append_phase(points, _interpolate(current, step1_end), offset)
    offset = _append_phase(points, _interpolate(step1_end, step2_end), offset)
    offset = _append_phase(points, _interpolate(step2_end, step3_end), offset)
    offset = _append_phase(points, _interpolate(step3_end, step4_end), offset)
    offset = _append_phase(points, _interpolate(step4_end, target), offset)

    if not points:
        return []
    # Guarantee the very last point lands exactly on target, same
    # reasoning as generate_synchronized_trajectory's final snap.
    points[-1] = TrajectoryPoint(
        t=points[-1].t, x=target.x, y=target.y, angle=target.angle
    )
    return points


def generate_detach_and_ensayo_trajectory(
    current: Position,
    tara_y: float,
    lift_above_tara_cm: float,
    x_shift_cm: float,
    ensayo_points: List[TrajectoryPoint],
    max_speed_x_cm_s: float,
    max_speed_y_cm_s: float,
    calibration_space: CalibrationSpace,
) -> List[TrajectoryPoint]:
    """
    "Platform detach" hop, FUSED directly onto the ensayo's own points
    into ONE continuous trajectory — run right before Run whenever a
    tara (basal, no-contact reference) has been recorded for the loaded
    ensayo, see TrajectoryScreen._send_and_check(), the only sanctioned
    caller, and the contact-threshold testing workflow it supports: an
    operator manually lowers Y in small increments from the tara
    reference to find the force platform's contact threshold, then
    presses Run at that (already touching) Y. Running straight from
    there would start the gait trajectory — and its force trace — with
    contact already established. This trajectory instead lifts clear of
    the platform, shifts sideways to fully detach, then swings back
    down into the EXACT same (x, y, angle) `current` position the
    operator was at, so the platform arrives back in contact already IN
    MOTION — and, since `ensayo_points` is appended directly onto the
    SAME trajectory rather than sent as a second transfer, the real
    gait motion begins the INSTANT the hop's last point is reached, not
    however long a second TRAJ_BEGIN/TRAJ_POINT.../TRAJ_END transfer
    over serial happens to take.

    Replaces the old two-trajectory version (generate_detach_trajectory,
    removed 2026-09-18) — that one was sent/run UNTIMED, blocked until
    FINISHED, and only THEN was the ensayo transferred and run as a
    SEPARATE trajectory. That gap between "the hop finishes" and "the
    ensayo actually starts moving" was the whole transfer time of the
    (potentially hundreds-of-points) ensayo — a real, visible dead
    pause on the rig. Fusing them into one TIMED send + one RUN removes
    that gap entirely: there is no second transfer to wait for, because
    there is no second transfer.

    Two synchronized (multi-axis-at-once) legs for the hop itself, same
    "en el mismo tiempo" requirement Luis specified for the return leg
    as the version this replaces:

        1. Lift + shift back: `current` -> (current.x - x_shift_cm,
           min(tara_y + lift_above_tara_cm, calibration_space.y_max),
           current.angle). Y and X move together, angle unchanged —
           unlike generate_safe_return_trajectory, this never touches
           angle, so there is no ANGLE_REFERENCE_DEG-style constraint
           forcing X and Y apart here.
        2. Shift forward + descend: -> `current`. Symmetric return.

    The hop's own duration is computed from `max_speed_x_cm_s`/
    `max_speed_y_cm_s` directly (see _fastest_leg), NOT from the
    ensayo's own time_scale-stretched pace — Luis's explicit revision,
    2026-09-18 (the first version shared the ensayo's time_scale, per
    his own earlier request, but a 1cm hop stretched by e.g. 30x felt
    "bastante lento"; he asked for it to be fast but still bounded by
    the real speed limits, not unbounded). Pass
    `SystemStateMachine.MAX_SPEED_X_CM_S`/`MAX_SPEED_Y_CM_S` scaled by
    `_DETACH_HOP_SPEED_SAFETY_MARGIN` (see that constant) — since the
    combined trajectory is sent TIMED, the hop is still subject to the
    real ceiling check (SystemStateMachine._points_to_step_deltas) as a
    backstop regardless of what duration this function computes; the
    margin exists so a correctly-computed hop doesn't get rejected by
    its own rounding.

    `lift_above_tara_cm`/`x_shift_cm` are Luis's fixed constants (see
    SystemStateMachine.DETACH_LIFT_ABOVE_TARA_CM/DETACH_X_SHIFT_CM).
    The lift target is capped at `calibration_space.y_max` (graceful
    cap, same pattern as generate_safe_return_trajectory's lift_y) and
    the shift target at `calibration_space.x_min` (always 0.0, X never
    goes negative) — defensive backstops only; in normal operation
    (per Luis) real trials stay well clear of X=0, so this cap is not
    expected to actually bind. `current` is NOT validated against
    `calibration_space` (it is the system's real, already-reached
    position, same reasoning as generate_safe_return_trajectory) — the
    CALLER is responsible for validating the full returned trajectory
    (hop + ensayo) before sending, same as it already did for the
    ensayo alone.

    `ensayo_points` is assumed to already start (at its own t=0)
    exactly at `current`/`tara_y` — true whenever the operator's
    confirmed initial position matches the recorded tara, which this
    whole workflow already assumes (unchanged from the version this
    replaces). Its own t=0 boundary point is dropped when stitched (see
    _append_phase) — same de-duplication as every other multi-phase
    generator here — so only its OWN x/y/angle values determine where
    the combined trajectory actually ends; nothing here re-snaps or
    re-validates them.

    Returns:
        A single continuous List[TrajectoryPoint]: the 2-leg hop
        followed by `ensayo_points`, t starting at 0. Empty only in the
        degenerate case where the hop is a no-op AND `ensayo_points` is
        empty.
    """
    lift_y = min(tara_y + lift_above_tara_cm, calibration_space.y_max)
    shifted_x = max(current.x - x_shift_cm, calibration_space.x_min)
    detached = Position(shifted_x, lift_y, current.angle)

    fast_x = max_speed_x_cm_s * _DETACH_HOP_SPEED_SAFETY_MARGIN
    fast_y = max_speed_y_cm_s * _DETACH_HOP_SPEED_SAFETY_MARGIN

    points: List[TrajectoryPoint] = []
    offset = 0.0
    offset = _append_phase(points, _fastest_leg(current, detached, fast_x, fast_y), offset)
    offset = _append_phase(points, _fastest_leg(detached, current, fast_x, fast_y), offset)
    offset = _append_phase(points, ensayo_points, offset)
    return points


def generate_variability_point_trajectory(
    current: Position,
    tara: Position,
    depth_cm: float,
    calibration_space: CalibrationSpace,
    lift_margin_cm: float,
    angle_reference_deg: float,
) -> List[TrajectoryPoint]:
    """
    Single test point for the height-variability/repeatability test
    matrix (see SystemStateMachine.go_to_variability_point(), the
    only sanctioned caller) — descends from the recorded `tara` (basal,
    no-contact reference, see tara_library.py) by `depth_cm` (0 for the
    tara row itself; negative for a row below it, e.g. -0.1cm for the
    "-1mm" row) and stops there, so the test point's own success/
    failure reflects that one specific depth in isolation.

    Two legs — same "always start from the same reference" reasoning as
    generate_detach_and_ensayo_trajectory's hop, so every sample at
    every depth approaches from the SAME starting point (`tara`) rather
    than from wherever the rig happened to end up after the previous
    sample, results stay comparable across repeats/depths:

        1. `current` -> `tara`: reached via the SAME safe repositioning
           sequence as "Reiniciar Ensayo"/"Ir a Posición Inicial" (see
           generate_safe_return_trajectory) instead of a direct
           diagonal move — Luis's explicit request, 2026-09-18: a
           matrix point must be gated by the same physical safety
           rules as every other repositioning in this app (lift clear
           before X travels, only move X at `angle_reference_deg`),
           not skip them just because it's a short calibration-style
           move. `floor_y` for that sequence is `tara.y` itself — never
           drop below the tara reference while X/angle are still in
           transit. The actual descent BELOW tara only happens in leg
           2, once already exactly there.
        2. `tara` -> (tara.x, tara.y + depth_cm, tara.angle): the
           actual depth-test descent (Y only, X/angle already match) —
           plain, no lift needed, this is the one place Y is
           INTENTIONALLY taken below the tara floor.

    Only the final depth target is validated directly here — `tara` is
    validated inside generate_safe_return_trajectory (its own `target`
    param), `current` is assumed already-real/already-reached (same
    reasoning as generate_detach_and_ensayo_trajectory's `current`),
    consistent with `tara` always being a value recorded from an actual
    GET_POSITION.

    Raises:
        PositionOutOfRangeError: If the resulting target (tara.y +
            depth_cm) falls outside `calibration_space`. No points are
            generated in that case.

    Returns:
        A single continuous List[TrajectoryPoint] spanning both legs, t
        starting at 0. Non-empty even when `current` already equals
        `tara` and `depth_cm` is 0 — same reasoning as
        generate_safe_return_trajectory: the lift+descend of leg 1
        still runs (whenever `lift_margin_cm` > 0) so every sample
        takes the exact same physical path, which is the point of a
        repeatability test.
    """
    target = Position(tara.x, tara.y + depth_cm, tara.angle)
    validate_position(target, calibration_space)

    safe_leg = generate_safe_return_trajectory(
        current, tara, tara.y, calibration_space, lift_margin_cm, angle_reference_deg,
    )
    descent_leg = _interpolate(tara, target)
    return stitch_trajectories([safe_leg, descent_leg])


def generate_angle_alignment_trajectory(
    current: Position,
    target_angle: float,
    time_scale: float,
    calibration_space: CalibrationSpace,
) -> List[TrajectoryPoint]:
    """
    Single angle-only synchronized leg: `current` -> (current.x,
    current.y, target_angle) — X/Y untouched. Used by
    TrajectoryScreen._send_and_check() to physically rotate the
    platform to the loaded ensayo's own recorded starting angle before
    the ensayo runs (Luis's explicit request, 2026-09-18): angle used
    to work by OFFSET (see _offset_points()'s own docstring — whatever
    angle the operator manually set at "Ir a Posición Inicial" became
    the ensayo's real starting angle, with the CSV's own recorded
    angle only used as a relative reference). Now the ensayo's own
    first CSV row is authoritative instead — the operator shouldn't
    have to manually type a matching angle. FUSED onto the same
    combined send as the ensayo (and the detach hop, if a tara is on
    record — see stitch_trajectories()) for the same "no extra
    transfer, no extra gap" reason as
    generate_detach_and_ensayo_trajectory.

    `time_scale` is the SAME factor already applied to the ensayo's
    own timeline, same reasoning as
    generate_detach_and_ensayo_trajectory's own `time_scale` param —
    kept consistent so every leg of one combined run moves at a
    coherent speed and is checked against the same
    MAX_SPEED_ANGLE_DEG_S ceiling once sent TIMED.

    Empty list if `target_angle` is already within `current.angle`'s
    negligible-move threshold (see _interpolate's `_MIN_MOVE_DISTANCE`)
    — Luis's explicit choice: skip the leg entirely rather than send a
    zero/near-zero rotation, but correct for ANY real difference, no
    matter how small.

    Raises:
        PositionOutOfRangeError: If `target_angle` falls outside
            `calibration_space.angle_min`/`angle_max`.
    """
    target = Position(current.x, current.y, target_angle)
    validate_position(target, calibration_space)
    return _interpolate(current, target, time_scale)


def stitch_trajectories(phases: List[List[TrajectoryPoint]]) -> List[TrajectoryPoint]:
    """
    Concatenate multiple already-generated phases (each starting at its
    own t=0) into one continuous trajectory with a single, strictly
    increasing time base — the same stitching _append_phase already
    does internally for every multi-leg generator in this module
    (generate_safe_return_trajectory, generate_detach_and_ensayo_trajectory,
    generate_variability_point_trajectory), exposed here as a small
    public utility so a caller can compose INDEPENDENTLY-generated
    pieces (e.g. TrajectoryScreen._send_and_check() putting an
    angle-alignment leg ahead of generate_detach_and_ensayo_trajectory's
    own output) without reaching into this module's private internals.

    Empty phases are skipped (no-op). Returns [] if every phase is
    empty, or if `phases` itself is empty.
    """
    points: List[TrajectoryPoint] = []
    offset = 0.0
    for phase in phases:
        offset = _append_phase(points, phase, offset)
    return points
