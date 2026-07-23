"""
serial_manager.py

Low-level serial transport layer between the Raspberry Pi and the ESP32.

Single responsibility: open/close the serial port, send lines of text,
and continuously read incoming lines on a background thread, delivering
each complete line to a registered callback.

This module has NO knowledge of the communication protocol (commands,
responses, trajectories). See protocol.py for that. This separation
means the transport (USB Serial today) can be replaced in the future
(e.g. Bluetooth, TCP) by rewriting only this file.
"""

import logging
import os
import threading
import time
from typing import Callable, Optional

import serial
from serial import SerialException

# Every line sent/received is logged here (with millisecond timestamps)
# so wire-level behavior can be inspected after the fact — added
# specifically to debug a state-desync symptom (RPi believes PAUSED,
# ESP32 rejects ABORT as INVALID_STATE) that can't be reproduced in a
# dev environment without the real ESP32. Logs to both console and
# logs/serial.log; check the log after reproducing an issue rather than
# guessing at timing blind.
LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "logs")
LOG_FILE = os.path.join(LOG_DIR, "gaitsim.log")
os.makedirs(LOG_DIR, exist_ok=True)

# Both this module and system_state.py log to LOG_FILE from different
# threads (serial reader thread vs. main/state thread). LOG_MAX_LINES
# and _log_lock are shared (system_state.py imports LineCappedFileHandler
# rather than building its own) so the trim below can't race between the
# two handler instances.
LOG_MAX_LINES = 500
_log_lock = threading.Lock()
_log_line_count = None


def _init_log_line_count() -> None:
    global _log_line_count
    if _log_line_count is None:
        try:
            with open(LOG_FILE, "r") as f:
                _log_line_count = sum(1 for _ in f)
        except FileNotFoundError:
            _log_line_count = 0


class LineCappedFileHandler(logging.FileHandler):
    """FileHandler that trims LOG_FILE to the last LOG_MAX_LINES lines
    whenever it grows past the cap, so long debugging sessions don't
    fill the disk with wire-level/state logs."""

    def emit(self, record: logging.LogRecord) -> None:
        global _log_line_count
        super().emit(record)
        with _log_lock:
            _init_log_line_count()
            _log_line_count += 1
            if _log_line_count > LOG_MAX_LINES:
                self.flush()
                with open(self.baseFilename, "r") as f:
                    lines = f.readlines()
                trimmed = lines[-LOG_MAX_LINES:]
                with open(self.baseFilename, "w") as f:
                    f.writelines(trimmed)
                _log_line_count = len(trimmed)


_logger = logging.getLogger("gaitsim.serial")
_logger.setLevel(logging.DEBUG)
if not _logger.handlers:
    _formatter = logging.Formatter(
        "%(asctime)s.%(msecs)03d %(message)s", datefmt="%H:%M:%S"
    )
    _file_handler = LineCappedFileHandler(LOG_FILE)
    _file_handler.setFormatter(_formatter)
    _console_handler = logging.StreamHandler()
    _console_handler.setFormatter(_formatter)
    _logger.addHandler(_file_handler)
    _logger.addHandler(_console_handler)
    _logger.propagate = False


class SerialManagerError(Exception):
    """Base exception for all SerialManager failures."""
    pass


class ConnectionFailedError(SerialManagerError):
    """Raised when the serial port could not be opened."""
    pass


class NotConnectedError(SerialManagerError):
    """Raised when attempting to send data while not connected."""
    pass


class SerialManager:
    """
    Manages a serial connection to the ESP32.

    Usage:
        manager = SerialManager(port="/dev/ttyUSB0", baudrate=115200)
        manager.on_line_received = my_callback_function
        manager.connect()
        manager.send_line("<PING>")
        ...
        manager.disconnect()

    Threading model:
        connect() starts a background daemon thread that continuously
        reads from the serial port and splits incoming bytes into lines
        (terminated by '\\n' or '\\r'). Each complete line is passed to
        on_line_received, if set. This callback runs on the background
        thread, NOT the main/UI thread — callers that need to update UI
        elements must marshal back to the main thread themselves (this
        will be handled by ESP32Controller / the UI layer, not here).
    """

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 1.0):
        """
        Args:
            port: Serial device path, e.g. "/dev/ttyUSB0" or "/dev/ttyACM0".
            baudrate: Must match the firmware's Serial.begin() value (115200
                      per the project protocol specification).
            timeout: Read timeout in seconds for the underlying pyserial
                     connection. Does not block indefinitely if the ESP32
                     stops responding.
        """
        self._port = port
        self._baudrate = baudrate
        self._timeout = timeout

        self._serial: Optional[serial.Serial] = None
        self._read_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._write_lock = threading.Lock()

        # Set this from outside to receive incoming lines.
        # Signature: callback(line: str) -> None
        self.on_line_received: Optional[Callable[[str], None]] = None

        # Set this from outside to be notified of unexpected disconnection.
        # Signature: callback() -> None
        self.on_disconnected: Optional[Callable[[], None]] = None

    @property
    def is_connected(self) -> bool:
        """True if the serial port is currently open."""
        return self._serial is not None and self._serial.is_open

    def connect(self) -> None:
        """
        Open the serial port and start the background read thread.

        Raises:
            ConnectionFailedError: If the port cannot be opened (e.g.
                wrong path, device not connected, permission denied).
        """
        if self.is_connected:
            return  # Already connected; calling again is a no-op.

        try:
            self._serial = serial.Serial(
                port=self._port,
                baudrate=self._baudrate,
                timeout=self._timeout,
            )
        except SerialException as exc:
            raise ConnectionFailedError(
                f"Could not open serial port '{self._port}': {exc}"
            ) from exc

        self._stop_event.clear()
        self._read_thread = threading.Thread(
            target=self._read_loop,
            daemon=True,  # Thread dies automatically when the program exits.
            name="SerialManager-ReadThread",
        )
        self._read_thread.start()
        _logger.debug("=== connected: %s @ %d ===", self._port, self._baudrate)

    def disconnect(self) -> None:
        """
        Stop the read thread and close the serial port.

        Safe to call even if not connected (no-op in that case).
        """
        self._stop_event.set()

        if self._read_thread is not None and self._read_thread.is_alive():
            self._read_thread.join(timeout=2.0)

        if self._serial is not None and self._serial.is_open:
            self._serial.close()

        self._serial = None
        self._read_thread = None

    def send_line(self, line: str) -> None:
        """
        Send a line of text to the ESP32.

        Args:
            line: The text to send, exactly as it should go on the wire.
                  Should already include any protocol framing required
                  (protocol.py's build_* functions already wrap RPi ->
                  ESP32 commands in '<' '>' delimiters).

        Raises:
            NotConnectedError: If the port is not currently open.
            SerialManagerError: If the write fails for any other reason
                (e.g. device unplugged mid-write).
        """
        if not self.is_connected:
            raise NotConnectedError(
                "Cannot send data: serial port is not connected."
            )

        try:
            # Lock prevents interleaved writes if send_line() is ever
            # called from multiple threads (e.g. UI thread + a future
            # background task) at the same time.
            with self._write_lock:
                self._serial.write(line.encode("ascii"))
                _logger.debug(">> %s", line.strip())
        except SerialException as exc:
            raise SerialManagerError(f"Failed to write to serial port: {exc}") from exc

    def _read_loop(self) -> None:
        """
        Background thread target: continuously read bytes from the
        serial port, accumulate them into lines, and dispatch complete
        lines to on_line_received.

        Runs until _stop_event is set (via disconnect()) or an
        unrecoverable read error occurs (in which case on_disconnected
        is invoked, if set).
        """
        buffer = ""

        while not self._stop_event.is_set():
            try:
                # in_waiting avoids blocking read() when there is
                # nothing to read yet; the outer while loop still
                # respects the configured timeout via readline().
                raw = self._serial.readline()
            except SerialException:
                # Port likely unplugged or became invalid mid-read.
                if self.on_disconnected is not None:
                    self.on_disconnected()
                return
            except (TypeError, AttributeError):
                # self._serial became None concurrently (disconnect()
                # was called from another thread while reading).
                return

            if not raw:
                # Timeout with no data; loop again and check stop_event.
                continue

            try:
                text = raw.decode("ascii", errors="replace")
            except Exception:
                continue

            buffer += text
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if line:
                    _logger.debug("<< %s", line)
                    if self.on_line_received is not None:
                        self.on_line_received(line)
