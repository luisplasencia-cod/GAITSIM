"""
variability_library.py

Manages the height-variability/repeatability test matrix data — see
SystemStateMachine.go_to_variability_point() and
VariabilityMatrixScreen. One JSON file per trajectory (CSV) id under a
directory, same one-file-per-id convention as tara_library.py/
position_library.py. Columns of the matrix (talla, subject/prosthesis
height) come from trajectory_library.parse_talla_cm() on the id; rows
are the fixed DEPTH_ROWS_MM offsets below that ensayo's recorded tara
(see tara_library.py) — row 0 is the tara itself.

File shape:
    {
        "rows": {
            "0": [{"y_real": .., "success": true, "timestamp": ".."}, ...],
            "-1": [...], "-2": [...], "-3": [...], "-4": [...], "-5": [...]
        }
    }

Deliberately plain data with no Qt dependency, same spirit as
tara_library.py — read/written directly by VariabilityMatrixScreen.
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List

from src.controllers import tara_library

DEFAULT_VARIABILITY_DIR = "data/variabilidad"

# Fixed rows: the tara itself (0) plus 5mm of depth below it, one row
# per millimeter — Luis's spec (2026-09-18): "-1 -1 -1 -1 -1", i.e. 5
# rows below the tara.
DEPTH_ROWS_MM: tuple = (0, -1, -2, -3, -4, -5)

# Repeatability sample count per cell — Luis's spec. Not hard-enforced
# (see add_sample) — a cell can hold more if a bad sample needs a
# retry beyond the 5th without losing the earlier ones — only used as
# the matrix screen's "n/5" display target.
SAMPLES_PER_CELL = 5


class VariabilityLibraryError(Exception):
    """Base exception for all variability_library failures."""
    pass


@dataclass
class Sample:
    y_real: float
    success: bool
    timestamp: str


@dataclass
class MatrixRecord:
    trajectory_id: str
    rows: Dict[int, List[Sample]] = field(
        default_factory=lambda: {mm: [] for mm in DEPTH_ROWS_MM}
    )


def _path(trajectory_id: str, directory: str) -> str:
    return os.path.join(directory, f"{trajectory_id}.json")


def _save(record: MatrixRecord, directory: str) -> None:
    os.makedirs(directory, exist_ok=True)
    with open(_path(record.trajectory_id, directory), "w") as f:
        json.dump({
            "rows": {
                str(depth_mm): [
                    {"y_real": s.y_real, "success": s.success, "timestamp": s.timestamp}
                    for s in samples
                ]
                for depth_mm, samples in record.rows.items()
            },
        }, f, indent=2)


def load_matrix(
    trajectory_id: str, directory: str = DEFAULT_VARIABILITY_DIR
) -> MatrixRecord:
    """
    Load `trajectory_id`'s matrix data, or an empty one (all
    DEPTH_ROWS_MM present, no samples) if none has been recorded yet.

    One-time bootstrap: if no matrix file exists yet but
    tara_library.py already has legacy contact-test "pruebas" recorded
    for this id (from before this matrix existed — always implicitly
    at the tara itself, since that workflow had no depth concept), they
    are migrated into row 0 (marked successful — the old pre-2026-09-18
    Run-with-detach flow only ever recorded a prueba after its detach
    move completed without raising) so real hardware data already
    captured doesn't just disappear from the app. Only persisted to disk (and only then
    idempotent on later calls) if there was actually something to
    migrate — an ensayo with no legacy pruebas gets a fresh in-memory
    empty record each time, same as tara_library.py not writing a file
    until first used.
    """
    path = _path(trajectory_id, directory)
    if os.path.isfile(path):
        with open(path) as f:
            data = json.load(f)
        rows = {mm: [] for mm in DEPTH_ROWS_MM}
        for depth_str, samples in data.get("rows", {}).items():
            rows[int(depth_str)] = [
                Sample(y_real=s["y_real"], success=s["success"], timestamp=s.get("timestamp", ""))
                for s in samples
            ]
        return MatrixRecord(trajectory_id=trajectory_id, rows=rows)

    record = MatrixRecord(trajectory_id=trajectory_id)
    try:
        tara_record = tara_library.load_tara(trajectory_id)
    except tara_library.TaraNotFoundError:
        tara_record = None
    if tara_record is not None and tara_record.pruebas:
        record.rows[0] = [
            Sample(y_real=p.y, success=True, timestamp=p.timestamp)
            for p in tara_record.pruebas
        ]
        _save(record, directory)
    return record


def add_sample(
    trajectory_id: str, depth_mm: int, y_real: float, success: bool,
    directory: str = DEFAULT_VARIABILITY_DIR,
) -> None:
    """
    Append one executed sample — recorded once the ENSAYO's own Run
    (not just SystemStateMachine.go_to_variability_point()'s
    repositioning move) finishes, see VariabilityMatrixScreen's
    "pending sample" tracking (2026-09-18 correction) — to
    `trajectory_id`'s row for `depth_mm`.

    Raises:
        VariabilityLibraryError: If `depth_mm` is not one of
            DEPTH_ROWS_MM.
    """
    if depth_mm not in DEPTH_ROWS_MM:
        raise VariabilityLibraryError(
            f"depth_mm={depth_mm} no es una fila válida de la matriz "
            f"({DEPTH_ROWS_MM})."
        )
    record = load_matrix(trajectory_id, directory)
    record.rows[depth_mm].append(
        Sample(y_real=y_real, success=success, timestamp=datetime.now().isoformat(timespec="seconds"))
    )
    _save(record, directory)


def delete_sample(
    trajectory_id: str, depth_mm: int, index: int,
    directory: str = DEFAULT_VARIABILITY_DIR,
) -> None:
    """
    Remove one sample (by its 0-based position within `depth_mm`'s
    list) from `trajectory_id`'s matrix — lets Luis discard a bad/
    mistaken execution from the "PUNTO SELECCIONADO" detail table
    (VariabilityMatrixScreen's per-row "Eliminar" button, 2026-09-18)
    without losing the rest of that cell's samples.

    Raises:
        VariabilityLibraryError: If `depth_mm` is not one of
            DEPTH_ROWS_MM, or `index` is out of range for that row's
            current sample list.
    """
    if depth_mm not in DEPTH_ROWS_MM:
        raise VariabilityLibraryError(
            f"depth_mm={depth_mm} no es una fila válida de la matriz "
            f"({DEPTH_ROWS_MM})."
        )
    record = load_matrix(trajectory_id, directory)
    samples = record.rows[depth_mm]
    if not 0 <= index < len(samples):
        raise VariabilityLibraryError(
            f"No hay una muestra #{index + 1} para talla '{trajectory_id}' "
            f"a {depth_mm}mm (solo hay {len(samples)})."
        )
    del samples[index]
    _save(record, directory)
