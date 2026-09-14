"""
tara_library.py

Manages per-ensayo "tara" (basal / no-contact reference) records for
the force-platform contact-threshold testing workflow (see
SystemStateMachine.perform_pre_run_detach() and TrajectoryScreen's
Tara button/history screen). One JSON file per trajectory (CSV) id
under a directory, mirroring position_library.py's convention —
{trajectory_id}.json holds that CSV's tara position plus every
recorded "prueba" (a contact test actually run: the Y height the
operator descended to before pressing Run).

File shape:
    {
        "tara": {"x": .., "y": .., "angle": .., "timestamp": "..."},
        "pruebas": [{"y": .., "timestamp": "..."}, ...]
    }

Deliberately plain data with no Qt dependency, same spirit as
position_library.py/trajectory_library.py — read/written directly by
TrajectoryScreen and the tara history screen.
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import List

from src.communication.protocol import Position

DEFAULT_TARA_DIR = "data/tara"


class TaraLibraryError(Exception):
    """Base exception for all tara_library failures."""
    pass


class TaraNotFoundError(TaraLibraryError):
    """No saved tara matches the given trajectory id."""
    pass


@dataclass
class Prueba:
    y: float
    timestamp: str


@dataclass
class TaraRecord:
    trajectory_id: str
    tara: Position
    tara_timestamp: str
    pruebas: List[Prueba] = field(default_factory=list)


def _path(trajectory_id: str, directory: str) -> str:
    return os.path.join(directory, f"{trajectory_id}.json")


def list_tara_ids(directory: str = DEFAULT_TARA_DIR) -> List[str]:
    """
    List the trajectory ids that have a saved tara, in this directory.
    Empty list if the directory doesn't exist yet — a normal condition.
    """
    if not os.path.isdir(directory):
        return []
    return [
        os.path.splitext(filename)[0]
        for filename in os.listdir(directory)
        if filename.lower().endswith(".json")
    ]


def has_tara(trajectory_id: str, directory: str = DEFAULT_TARA_DIR) -> bool:
    return os.path.isfile(_path(trajectory_id, directory))


def load_tara(trajectory_id: str, directory: str = DEFAULT_TARA_DIR) -> TaraRecord:
    """
    Raises:
        TaraNotFoundError: If no tara file matches trajectory_id.
    """
    path = _path(trajectory_id, directory)
    if not os.path.isfile(path):
        raise TaraNotFoundError(
            f"No hay tara guardada para '{trajectory_id}' en '{directory}'."
        )
    with open(path) as f:
        data = json.load(f)
    tara_data = data["tara"]
    return TaraRecord(
        trajectory_id=trajectory_id,
        tara=Position(x=tara_data["x"], y=tara_data["y"], angle=tara_data["angle"]),
        tara_timestamp=tara_data.get("timestamp", ""),
        pruebas=[
            Prueba(y=p["y"], timestamp=p.get("timestamp", ""))
            for p in data.get("pruebas", [])
        ],
    )


def save_tara(
    trajectory_id: str, position: Position, directory: str = DEFAULT_TARA_DIR
) -> None:
    """
    Save (or overwrite) the tara for `trajectory_id`. Overwriting an
    existing tara is the "corrección futura" flow Luis asked for — the
    CALLER (TrajectoryScreen) is responsible for confirming the
    overwrite with the operator before calling this; this function
    itself always overwrites unconditionally. Existing `pruebas` are
    preserved (only the `tara` block is replaced) — correcting a tara
    doesn't erase the test history already recorded against it.
    """
    os.makedirs(directory, exist_ok=True)
    path = _path(trajectory_id, directory)
    pruebas = []
    if os.path.isfile(path):
        with open(path) as f:
            pruebas = json.load(f).get("pruebas", [])
    with open(path, "w") as f:
        json.dump({
            "tara": {
                "x": position.x, "y": position.y, "angle": position.angle,
                "timestamp": datetime.now().isoformat(timespec="seconds"),
            },
            "pruebas": pruebas,
        }, f, indent=2)


def add_prueba(
    trajectory_id: str, y: float, directory: str = DEFAULT_TARA_DIR
) -> None:
    """
    Append a contact-test record (the Y value actually run, captured
    right after SystemStateMachine.perform_pre_run_detach() lands back
    at it) to `trajectory_id`'s existing tara file.

    Raises:
        TaraNotFoundError: If no tara exists yet for trajectory_id —
            callers only reach this once a tara has already been
            confirmed to exist (see TrajectoryScreen's detach branch).
    """
    record = load_tara(trajectory_id, directory)
    record.pruebas.append(
        Prueba(y=y, timestamp=datetime.now().isoformat(timespec="seconds"))
    )
    path = _path(trajectory_id, directory)
    with open(path, "w") as f:
        json.dump({
            "tara": {
                "x": record.tara.x, "y": record.tara.y, "angle": record.tara.angle,
                "timestamp": record.tara_timestamp,
            },
            "pruebas": [{"y": p.y, "timestamp": p.timestamp} for p in record.pruebas],
        }, f, indent=2)


def load_all(directory: str = DEFAULT_TARA_DIR) -> List[TaraRecord]:
    """All tara records, sorted by trajectory id — feeds the tara
    history ("hoja de cálculo") screen."""
    return [load_tara(tid, directory) for tid in sorted(list_tara_ids(directory))]
