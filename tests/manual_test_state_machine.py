"""
Manual verification script for SystemStateMachine.

Updated 2026-09-02: move_manual()/move_manual_stop() (raw, un-bounded
step moves — see can_move_manually_raw()'s docstring) are now gated to
BEFORE calibration (state DISCONNECTED, i.e. connected but not yet
homed), not IDLE — the opposite of before, when they were dead code
gated the same as the calibrated joystick (can_move_manually()/IDLE).
Reconciled against the real firmware's MANUAL command, which has no
HOME requirement at all (see docs/protocol.md, "Cambio 2026-09-02").

Usage:
    python3 -m tests.manual_test_state_machine
"""

import time
from src.communication.esp32_controller import ESP32Controller
from src.communication.protocol import TrajectoryPoint
from src.controllers.system_state import SystemStateMachine, InvalidTransitionError


def on_state_changed(new_state):
    print(f">>> STATE CHANGED: {new_state.name}")


def main():
    controller = ESP32Controller(port="/dev/ttyUSB0")  # ajusta si es otro
    state_machine = SystemStateMachine(controller)
    state_machine.on_state_changed = on_state_changed

    controller.connect()
    time.sleep(2)

    print(f"Initial state: {state_machine.state.name}")
    print(f"Can move manually (raw, pre-calibración)? {state_machine.can_move_manually_raw()}")

    # This should now SUCCEED: connected but not yet homed is exactly
    # when the raw pre-calibration test move is meant to work.
    print("Trying raw manual move before HOME (should succeed)...")
    state_machine.move_manual("X", "+", 50)
    print("Raw manual move accepted.")
    print("Stopping it explicitly (MANUAL_STOP)...")
    state_machine.move_manual_stop()
    print("Stopped.")

    print("Homing...")
    state_machine.home()
    print(f"State after home: {state_machine.state.name}")
    print(f"Can move manually (calibrated joystick)? {state_machine.can_move_manually()}")
    print(f"Can move manually (raw, pre-calibración) now? {state_machine.can_move_manually_raw()}")

    # This should now fail: raw manual move is pre-calibration only —
    # once IDLE (homed), the calibrated move_relative() is the intended
    # path instead (needs a known InitialPositionSession/target to call,
    # not exercised standalone here).
    try:
        state_machine.move_manual("X", "+", 50)
    except InvalidTransitionError as e:
        print(f"Expected rejection (raw manual no longer allowed post-HOME): {e}")

    points = [
        TrajectoryPoint(t=0.0, x=0.0, y=0.0, angle=0.0),
        TrajectoryPoint(t=0.1, x=1.0, y=0.5, angle=2.0),
    ]
    print("Sending trajectory...")
    result = state_machine.send_trajectory(points)
    print(f"Transfer result: {result}")

    print("Running...")
    state_machine.run()
    print(f"State immediately after run(): {state_machine.state.name}")

    # This should fail: system is RUNNING.
    try:
        state_machine.move_manual("Y", "-", 20)
    except InvalidTransitionError as e:
        print(f"Expected rejection while running: {e}")

    time.sleep(2)  # allow FINISHED to arrive
    print(f"State after waiting for FINISHED: {state_machine.state.name}")

    controller.disconnect()


if __name__ == "__main__":
    main()