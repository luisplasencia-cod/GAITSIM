"""
action_worker.py

Shared minimal single-shot background worker: runs one blocking
state-machine action (home, move_relative, send_trajectory, etc.) on a
background thread so the UI stays responsive while waiting for the
ESP32's response/timeout.

Previously duplicated per-screen (ConnectionScreen, TrajectoryScreen)
with an explicit comment marking a 3rd consumer as the signal to
promote it here — ManualJoystickControl (manual_joystick.py) is that
3rd consumer.
"""

from PySide6.QtCore import QThread, Signal


class ActionWorker(QThread):
    """
    Intentionally minimal — a single-shot worker per action, not a
    persistent command queue. If a screen ever needs more sophisticated
    sequencing (e.g. trajectory transfer with progress), that will be
    designed separately rather than overloading this class.
    """
    succeeded = Signal()
    failed = Signal(str)

    def __init__(self, action_fn, parent=None):
        super().__init__(parent)
        self._action_fn = action_fn

    def run(self):
        try:
            self._action_fn()
            self.succeeded.emit()
        except Exception as exc:
            self.failed.emit(str(exc))
