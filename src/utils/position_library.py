"""
position_library.py

Manages a collection of named initial positions (x, y, angle), stored
as one JSON file per position under a directory, mirroring the
convention trajectory_library.py uses for trajectory CSVs. Lets
screens save/list/load a starting position by name instead of
re-entering coordinates every session.
"""

import json
import os

from src.communication.protocol import Position

DEFAULT_POSITIONS_DIR = "data/positions"


class PositionLibraryError(Exception):
    """Base exception for all position_library failures."""
    pass


class PositionNotFoundError(PositionLibraryError):
    """No saved position matches the given name."""
    pass


def list_positions(directory: str = DEFAULT_POSITIONS_DIR) -> list[str]:
    """
    List the names of all saved positions in the given directory.

    Returns an empty list if the directory does not exist or contains
    no saved positions yet — a normal condition, not an error.
    """
    if not os.path.isdir(directory):
        return []
    return [
        os.path.splitext(filename)[0]
        for filename in os.listdir(directory)
        if filename.lower().endswith(".json")
    ]


def save_position(
    name: str, position: Position, directory: str = DEFAULT_POSITIONS_DIR
) -> None:
    """
    Save a position under the given name, overwriting any existing
    saved position with the same name.
    """
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{name}.json")
    with open(path, "w") as f:
        json.dump({"x": position.x, "y": position.y, "angle": position.angle}, f)


def load_position(name: str, directory: str = DEFAULT_POSITIONS_DIR) -> Position:
    """
    Load a previously saved position by name.

    Raises:
        PositionNotFoundError: If no file matches the given name.
    """
    path = os.path.join(directory, f"{name}.json")
    if not os.path.isfile(path):
        raise PositionNotFoundError(
            f"No saved position named '{name}' in '{directory}'."
        )
    with open(path) as f:
        data = json.load(f)
    return Position(x=data["x"], y=data["y"], angle=data["angle"])
