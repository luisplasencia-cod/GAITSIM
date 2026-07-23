"""
trajectory_library.py

Manages a collection of trajectory CSV files stored in a directory,
allowing screens to list available trajectories and load one by a
simple identifier, without handling file paths directly.

Convention: every ".csv" file inside the trajectories directory is a
loadable trajectory. The filename (without the ".csv" extension) is
its identifier.
"""

import os

from src.communication.protocol import TrajectoryPoint
from src.utils.trajectory_loader import TrajectoryLoadError, load_trajectory

DEFAULT_TRAJECTORIES_DIR = "data/trajectories"


class TrajectoryNotFoundError(TrajectoryLoadError):
    """No trajectory file matches the given identifier."""
    pass


def list_trajectories(directory: str = DEFAULT_TRAJECTORIES_DIR) -> list[str]:
    """
    List the identifiers of all trajectories available in the given
    directory.

    Args:
        directory: Path to the directory containing trajectory CSV
                   files. Defaults to "data/trajectories".

    Returns:
        A list of trajectory identifiers (filenames without the ".csv"
        extension), in no particular guaranteed order. Returns an
        empty list if the directory does not exist or contains no
        CSV files — this is a normal condition, not an error (e.g.
        a fresh installation before any trajectory has been added).
    """
    if not os.path.isdir(directory):
        return []

    identifiers = [
        os.path.splitext(filename)[0]
        for filename in os.listdir(directory)
        if filename.lower().endswith(".csv")
    ]
    return identifiers


def get_trajectory_path(
    trajectory_id: str, directory: str = DEFAULT_TRAJECTORIES_DIR
) -> str:
    """
    Resolve a trajectory identifier to its full file path.

    Args:
        trajectory_id: The trajectory's identifier (filename without
                       the ".csv" extension), as returned by
                       list_trajectories().
        directory: Path to the directory containing trajectory CSV
                   files.

    Returns:
        The full path to the corresponding .csv file.

    Raises:
        TrajectoryNotFoundError: If no file matches the given
            identifier in the given directory.
    """
    candidate_path = os.path.join(directory, f"{trajectory_id}.csv")
    if not os.path.isfile(candidate_path):
        raise TrajectoryNotFoundError(
            f"No trajectory found with id '{trajectory_id}' in "
            f"'{directory}'."
        )
    return candidate_path


def load_trajectory_by_id(
    trajectory_id: str, directory: str = DEFAULT_TRAJECTORIES_DIR
) -> list[TrajectoryPoint]:
    """
    Resolve a trajectory identifier and load its full point sequence.

    Convenience function combining get_trajectory_path() and
    load_trajectory(), so callers (e.g. the UI layer) can request a
    trajectory purely by name.

    Args:
        trajectory_id: The trajectory's identifier.
        directory: Path to the directory containing trajectory CSV
                   files.

    Returns:
        The list of TrajectoryPoint for the requested trajectory.

    Raises:
        TrajectoryNotFoundError: If the identifier does not match any
            file.
        Any exception load_trajectory() may raise (MissingColumnError,
            MissingValueError, NonMonotonicTimeError,
            TrajectoryFileNotFoundError) if the file exists but is
            invalid.
    """
    path = get_trajectory_path(trajectory_id, directory)
    return load_trajectory(path)