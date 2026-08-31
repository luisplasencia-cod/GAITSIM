# Axis motion: session notes

This document summarizes the low-level motion block in `main.cpp`, centered on `axis_move_steps()` and the related timer ISR and stop functions.

## Purpose

The ESP32 controls three stepper axes: `axis_x`, `axis_y`, and `axis_k`. Each axis has a DIR pin, a STEP pin, a hardware timer, and minimum/maximum limit-switch inputs.

`axis_move_steps(Axis &ax, int direction, int steps, int steps_per_sec)` starts a finite move for one axis.

- It rejects an axis that is already moving (`steps_remaining != 0`).
- It rejects non-positive steps or speed and a direction value other than 0 or 1.
- It rejects movement toward a limit switch that is already pressed.
- It applies DIR, waits for the driver direction setup time, then arms the timer.
- The shared motion state (`motion_sign`, `steps_remaining`) and timer activation are protected with `axes_mux`.

The timer alarm period is half the requested step period. Therefore, each timer ISR alternates STEP high and low.

## Position accounting and timer ISR

`axis_isr_common(Axis &ax)` is called by `isr_x`, `isr_y`, or `isr_k`.

- On a rising edge, it drives STEP high.
- On a falling edge, it drives STEP low, applies `motion_sign` to `steps_position`, and decrements `steps_remaining`.
- A full step is counted only on the falling edge, so `steps_position` represents completed pulses.
- Once no steps remain, it disables and stops that axis timer.

The ISR enters `portENTER_CRITICAL_ISR(&axes_mux)`. This is required on the dual-core ESP32 because task code and an ISR can access the shared axis state from different cores. It also keeps all state transitions consistently protected.

## Stopping behavior

`axis_stop(Axis &ax)` is an internal helper that must be called while `axes_mux` is already held.

- If STEP is low (`step_state == 0`), it immediately disables/stops the timer and sets `steps_remaining = 0`.
- If STEP is high, it sets `stop_requested = 1`. The timer ISR performs the matching falling edge, accounts for that final completed step, then stops the timer.

This prevents leaving STEP high or counting a partial pulse.

`all_axes_stop()` acquires `axes_mux` and calls `axis_stop()` for X, Y, and K.

`isr_limit()` also acquires the ISR version of the same mutex, sets `limit_triggered = 1`, and calls `axis_stop()` for all axes. Any limit switch is therefore a global stop.

## Completion query

`all_axes_finished()` returns true when the three `steps_remaining` values are zero. It intentionally is not atomic.

This is acceptable under the project-level contract that only one high-level motion launcher may issue moves at a time. Each individual `int` read is atomic on ESP32, and during an already-started move the timer ISR only decreases the counters to zero.

If multiple high-level launchers are allowed, this function alone cannot reserve the axes. The high-level code must prevent concurrent commands (for example, manual/GOTO commands competing with the trajectory task).

## High-level contract still required

The low-level axis functions protect timer/ISR shared state; they do not arbitrate complete multi-axis commands.

The higher-level command/state logic must ensure:

1. Only one flow can start motion at a time.
2. A manual or GOTO command cannot start while a trajectory owns motion execution.
3. A three-axis command is not silently accepted as a partial start.

At present, `primary_task` can issue manual/GOTO commands while `auxiliar_task` can execute trajectory points. This command arbitration is a future high-level task, not a responsibility of `axis_move_steps()`.
