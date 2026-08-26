"""
Manual verification script for ESP32Controller.

Usage:
    python3 -m tests.manual_test_controller
"""

import time
from src.communication.esp32_controller import ESP32Controller
from src.communication.protocol import TrajectoryPoint


def on_finished():
    print(">>> Trajectory FINISHED (callback fired)")


def on_error(code, message):
    print(f">>> ERROR callback: {code} - {message}")


def main():
    controller = ESP32Controller(port="/dev/ttyUSB0")  # ajusta si es otro
    controller.on_trajectory_finished = on_finished
    controller.on_error = on_error

    print("Connecting...")
    controller.connect()
    time.sleep(2)  # boot settle time

    print("Pinging...")
    alive = controller.ping()
    print(f"Alive: {alive}")

    print("Homing...")
    controller.home()
    print("Homed successfully.")

    # Small fake trajectory: 3 points, just to exercise the transfer.
    points = [
        TrajectoryPoint(t=0.0, x=0.0, y=0.0, angle=0.0),
        TrajectoryPoint(t=0.1, x=1.0, y=0.5, angle=2.0),
        TrajectoryPoint(t=0.2, x=2.0, y=1.0, angle=4.0),
    ]

    print("Sending trajectory...")
    result = controller.send_trajectory(points)
    print(f"Transfer result: {result}")

    if result.success:
        print("Running...")
        controller.run()
        print("run() returned (should be immediate, not waiting for FINISHED).")

        # Give the simulated firmware time to finish and fire the callback.
        time.sleep(2)

    controller.disconnect()


if __name__ == "__main__":
    main()