"""
Manual/offline verification for the 2026-08-26 change to
ConnectionStatusButton (src/ui/status_indicator.py): clicking it while
disconnected now sends a PING after connect() and only treats the
connection as successful if PONG comes back — opening the serial port
alone doesn't confirm a real, responsive ESP32 is on the other end.

Uses a mocked ESP32Controller — no real hardware/serial connection
needed — same spirit as the mocked-controller verification already
used elsewhere in this project's history.

Usage:
    python3 -m tests.manual_test_connect_ping
"""

from unittest.mock import MagicMock

from PySide6.QtWidgets import QApplication

from src.communication.esp32_controller import ESP32Controller
from src.controllers.system_state import SystemState, SystemStateMachine
from src.ui.bridge import StateMachineBridge
from src.ui.status_indicator import ConnectionStatusButton
from src.ui.theme_manager import ThemeManager

_failures = 0


def check(label: str, condition: bool):
    global _failures
    status = "PASS" if condition else "FAIL"
    if not condition:
        _failures += 1
    print(f"{status}: {label}")


def make_button(ping_result: bool):
    controller = MagicMock(spec=ESP32Controller)
    controller.is_connected = False
    controller.ping.return_value = ping_result
    sm = SystemStateMachine(controller)
    bridge = StateMachineBridge(sm)
    button = ConnectionStatusButton(bridge, ThemeManager())
    return button, controller


def main():
    app = QApplication.instance() or QApplication([])

    # PING succeeds -> treated as a real successful connection.
    button, controller = make_button(ping_result=True)
    button._on_clicked()
    check("connect() was called", controller.connect.called)
    check("ping() was called after connect()", controller.ping.called)
    check("disconnect() NOT called on a successful ping", not controller.disconnect.called)
    check("button shows CONNECTED state after a successful ping", button._current_state_name == "CONNECTED")

    # PING fails (no PONG) -> connection is rolled back, NOT treated as connected.
    button2, controller2 = make_button(ping_result=False)
    button2._on_clicked()
    check("connect() was called", controller2.connect.called)
    check("ping() was called after connect()", controller2.ping.called)
    check("disconnect() IS called when ping fails (rolls back the open port)", controller2.disconnect.called)
    check("button shows ERROR state, NOT CONNECTED, when ping fails", button2._current_state_name == "ERROR")

    if _failures:
        print(f"\n{_failures} check(s) FAILED.")
        raise SystemExit(1)
    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
