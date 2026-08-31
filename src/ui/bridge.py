"""
bridge.py

Thread-safe bridge between SystemStateMachine (plain Python, callbacks
fired from the serial read thread) and the Qt UI (which requires all
widget updates to happen on the main thread).

SystemStateMachine and everything below it (ESP32Controller,
SerialManager, protocol.py) have NO knowledge of Qt. This is the only
file where that translation happens — if the UI framework ever
changes, only this file needs to be rewritten.
"""

from PySide6.QtCore import QObject, Signal

from src.controllers.system_state import SystemState, SystemStateMachine


class StateMachineBridge(QObject):
    """
    Wraps a SystemStateMachine, converting its Python callbacks into
    Qt signals that can be safely connected to UI slots regardless of
    which thread originally triggered the event.

    Usage:
        state_machine = SystemStateMachine(esp32_controller)
        bridge = StateMachineBridge(state_machine)

        bridge.state_changed.connect(my_widget.on_state_changed)
        bridge.trajectory_finished.connect(my_widget.on_finished)
        bridge.device_error.connect(my_widget.on_error)
        bridge.disconnected.connect(my_widget.on_disconnected)
    """

    # Signal payloads use plain types (str, not SystemState enum
    # directly) to keep this bridge easy to connect to from anywhere,
    # including future non-Qt consumers if ever needed.
    state_changed = Signal(str)          # emits SystemState.name
    trajectory_finished = Signal()
    trajectory_progress = Signal(float, float, float, float)  # t, x, y, angle
    device_error = Signal(str, str)      # code, message
    disconnected = Signal()
    connected = Signal()

    def __init__(self, state_machine: SystemStateMachine, parent=None):
        super().__init__(parent)
        self._state_machine = state_machine

        # Subscribe to the state machine's callbacks. Each one simply
        # emits the corresponding Qt signal — no UI logic lives here.
        self._state_machine.on_state_changed = self._on_state_changed
        self._state_machine.on_trajectory_finished = self._on_trajectory_finished
        self._state_machine.on_trajectory_progress = self._on_trajectory_progress

        # Also forward the underlying ESP32Controller's error/disconnect
        # events, since SystemStateMachine consumes them internally but
        # the UI still needs to know an error occurred (e.g. to show a
        # message), not just that the state fell back to IDLE.
        self._state_machine.controller.on_error = self._on_device_error
        self._state_machine.controller.on_disconnected = self._on_disconnected

    @property
    def state_machine(self) -> SystemStateMachine:
        """Expose the underlying state machine for screens that need
        to call actions (home(), move_manual(), etc.) directly."""
        return self._state_machine

    def notify_connected(self) -> None:
        """
        Called by UI code right after a successful ESP32Controller.connect().
        Connecting alone does not change SystemState (it stays DISCONNECTED
        until Home succeeds), so there is no state_changed callback to
        piggyback on — this lets UI elements like the status indicator
        reflect "connected, not yet homed" immediately anyway.
        """
        self.connected.emit()

    # ------------------------------------------------------------------
    # Callback handlers — these run on the serial read thread and must
    # do nothing except emit a signal (no widget access, no blocking).
    # ------------------------------------------------------------------

    def _on_state_changed(self, new_state: SystemState) -> None:
        self.state_changed.emit(new_state.name)

    def _on_trajectory_finished(self) -> None:
        self.trajectory_finished.emit()

    def _on_trajectory_progress(self, point) -> None:
        self.trajectory_progress.emit(point.t, point.x, point.y, point.angle)

    def _on_device_error(self, code: str, message: str) -> None:
        self.device_error.emit(code, message)

    def _on_disconnected(self) -> None:
        self.disconnected.emit()