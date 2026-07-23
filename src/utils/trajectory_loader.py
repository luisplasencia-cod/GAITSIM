"""
trajectory_loader.py

Reads a gait trajectory CSV file and converts it into a list of
TrajectoryPoint objects, ready to be sent to the ESP32 via
ESP32Controller.send_trajectory() / SystemStateMachine.send_trajectory().

Expected CSV format:
    - Semicolon-separated (";").
    - First row: header (column names vary by file/phase, e.g.
      "Tiempo_sagital_balanceo", "Posicion_cm_X_balanceo", ...).
    - First 4 columns, in order, are always: time, pos_x, pos_y, angle.
    - Any additional columns (e.g. trailing empty columns from the
      original export) are ignored.
    - Number of rows may vary between files.
"""

import pandas as pd

from src.communication.protocol import TrajectoryPoint


class TrajectoryLoadError(Exception):
    """Base exception for all trajectory loading failures."""
    pass


class TrajectoryFileNotFoundError(TrajectoryLoadError):
    """The specified CSV file does not exist or could not be read."""
    pass


class MissingColumnError(TrajectoryLoadError):
    """The file has fewer than the 4 required columns."""
    pass


class MissingValueError(TrajectoryLoadError):
    """The CSV contains missing (NaN) values in a required column."""
    pass


class NonMonotonicTimeError(TrajectoryLoadError):
    """The 'time' column is not strictly increasing."""
    pass


REQUIRED_COLUMN_COUNT = 4
INTERNAL_COLUMN_NAMES = ["time", "pos_x", "pos_y", "angle"]


def load_trajectory(csv_path: str, sep: str = ";") -> list[TrajectoryPoint]:
    """
    Load and validate a trajectory CSV file.

    Column names in the source file are not assumed to be fixed (they
    vary by trial/phase, e.g. "Tiempo_sagital_balanceo"). Instead, the
    first 4 columns are taken positionally, in the order:
    time, pos_x, pos_y, angle. Any further columns (e.g. trailing empty
    columns present in the original export) are ignored.

    Args:
        csv_path: Path to the CSV file.
        sep: Field separator used in the file. Defaults to ";", which
             matches the format currently exported by the project's
             data acquisition process.

    Returns:
        A list of TrajectoryPoint, one per CSV row, in file order.

    Raises:
        TrajectoryFileNotFoundError: If the file does not exist or
            cannot be read.
        MissingColumnError: If the file has fewer than 4 columns.
        MissingValueError: If any of the first 4 columns contain NaN
            values.
        NonMonotonicTimeError: If the time column is not strictly
            increasing (including repeated consecutive timestamps).
    """
    try:
        df = pd.read_csv(csv_path, sep=sep)
    except FileNotFoundError as exc:
        raise TrajectoryFileNotFoundError(
            f"Trajectory file not found: {csv_path}"
        ) from exc
    except Exception as exc:
        raise TrajectoryLoadError(
            f"Failed to read CSV file '{csv_path}': {exc}"
        ) from exc

    # 1. Validate there are at least 4 columns before touching them.
    if df.shape[1] < REQUIRED_COLUMN_COUNT:
        raise MissingColumnError(
            f"File '{csv_path}' has {df.shape[1]} column(s); "
            f"at least {REQUIRED_COLUMN_COUNT} are required "
            f"(time, pos_x, pos_y, angle, in that order)."
        )

    # 2. Take the first 4 columns positionally and rename them
    #    internally, regardless of their original header text.
    data = df.iloc[:, :REQUIRED_COLUMN_COUNT].copy()
    data.columns = INTERNAL_COLUMN_NAMES

    # 3. Validate no missing values in those 4 columns.
    if data.isna().any().any():
        raise MissingValueError(
            f"File '{csv_path}' contains missing values in one or more "
            f"of the first {REQUIRED_COLUMN_COUNT} columns."
        )

    # 4. Validate time is strictly increasing (no decreases, no repeats).
    if not data["time"].is_monotonic_increasing or data["time"].duplicated().any():
        raise NonMonotonicTimeError(
            f"File '{csv_path}': time column must be strictly "
            f"increasing (no repeats, no decreases)."
        )

    trajectory = [
        TrajectoryPoint(t=row.time, x=row.pos_x, y=row.pos_y, angle=row.angle)
        for row in data.itertuples(index=False)
    ]

    return trajectory