"""
Manual verification script for trajectory_library.

Usage:
    python3 -m tests.manual_test_trajectory_library
"""

from src.utils.trajectory_library import (
    list_trajectories,
    get_trajectory_path,
    load_trajectory_by_id,
    TrajectoryNotFoundError,
)


def main():
    print("Available trajectories:", list_trajectories())

    path = get_trajectory_path("balanceo_v4")
    print(f"Path for 'balanceo_v4': {path}")

    points = load_trajectory_by_id("balanceo_v4")
    print(f"Loaded {len(points)} points from 'balanceo_v4'")

    try:
        get_trajectory_path("no_existe")
    except TrajectoryNotFoundError as e:
        print(f"Expected error: {e}")


if __name__ == "__main__":
    main()