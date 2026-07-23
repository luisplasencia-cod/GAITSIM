"""
initial_position_session.py

Tracks the (x, y, angle) initial position currently set up for the next
trajectory run, and whether it corresponds to a saved file. Set up and
adjusted in ConnectionScreen (after HOME: load a saved position or type
one, optionally fine-tune it with the manual buttons), then read by
TrajectoryScreen to offset trajectory points and to decide, once a run
finishes, whether to offer saving it.

Owned once by main_window.py and passed to both screens — same shared-
ownership pattern as SystemStateMachine/StateMachineBridge. Deliberately
plain data with no Qt dependency, same spirit as SystemStateMachine.
"""

from dataclasses import dataclass
from typing import Optional

from src.communication.protocol import Position


@dataclass
class InitialPositionSession:
    """
    saved_name is set exactly once per Home cycle — either by loading a
    file (ConnectionScreen._load_position_by_name) or by a post-run save
    (TrajectoryScreen) — and stays fixed after that. It is deliberately
    NOT touched by set()/GOTO or by manual nudges: whether the current
    position still matches that saved file is instead checked directly
    against the file itself at save-prompt time (see TrajectoryScreen),
    rather than through a manually-maintained "dirty" flag — a flag like
    that is easy to leave stale on some code path and silently skip a
    save the user expected.
    """
    position: Optional[Position] = None
    saved_name: Optional[str] = None

    def set(self, position: Position) -> None:
        """Record the position now in effect — just physically reached
        via GOTO, or just updated by a manual nudge."""
        self.position = position

    def apply_manual_delta(self, axis: str, direction: str, amount: float) -> None:
        """
        Update the tracked position after a successful manual nudge
        (MOVE_REL), mirroring what the ESP32 just did. Requires a
        position to already be established (via GOTO) — the ConnectionScreen
        gates the manual-move buttons on that so this is never called
        while position is None.
        """
        if self.position is None:
            return
        delta = amount if direction == "+" else -amount
        x, y, angle = self.position.x, self.position.y, self.position.angle
        if axis == "X":
            x += delta
        elif axis == "Y":
            y += delta
        elif axis == "A":
            angle += delta
        self.set(Position(x=x, y=y, angle=angle))

    def reset(self) -> None:
        """Called after a fresh HOME — any previously tracked position
        is no longer meaningful once the reference frame is reset."""
        self.position = None
        self.saved_name = None
