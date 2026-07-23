"""
Manual verification script for SystemStateMachine.

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
    print(f"Can move manually? {state_machine.can_move_manually()}")

    # This should fail: not idle yet (still DISCONNECTED).
    try:
        state_machine.move_manual("X", "+", 50)
    except InvalidTransitionError as e:
        print(f"Expected rejection: {e}")

    print("Homing...")
    state_machine.home()
    print(f"State after home: {state_machine.state.name}")
    print(f"Can move manually now? {state_machine.can_move_manually()}")

    print("Trying manual move (should succeed now)...")
    state_machine.move_manual("X", "+", 50)
    print("Manual move accepted.")

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