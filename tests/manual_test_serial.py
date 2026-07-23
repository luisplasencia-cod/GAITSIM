"""
Manual verification script for SerialManager + ESP32 test firmware.

Not an automated test (no assertions) — run manually to confirm the
serial link works end-to-end before continuing development.

Usage:
    python3 tests/manual_test_serial.py
"""

import time
from src.communication.serial_manager import SerialManager


def on_line(line: str) -> None:
    print(f"RECEIVED: {line}")


def main():
    manager = SerialManager(port="/dev/ttyUSB0")  # ajusta si tu puerto es otro
    manager.on_line_received = on_line

    print("Connecting...")
    manager.connect()

    # Give the ESP32 time to finish its boot sequence after the port
    # opens (many boards reset when the serial connection is established).
    time.sleep(2)

    print("Sending PING...")
    manager.send_line("<PING>")
    time.sleep(1)

    print("Sending STATUS...")
    manager.send_line("<STATUS>")
    time.sleep(1)

    print("Disconnecting...")
    manager.disconnect()


if __name__ == "__main__":
    main()