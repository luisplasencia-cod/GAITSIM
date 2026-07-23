"""
Manual verification script for trajectory_loader.

Usage:
    python3 -m tests.manual_test_trajectory_loader
"""

from src.utils.trajectory_loader import load_trajectory


def main():
    csv_path = "data/test_trajectories/Control_balanceo_Luis_V4.csv"

    points = load_trajectory(csv_path)
    print(f"Total points: {len(points)}")
    print(f"First point: {points[0]}")
    print(f"Last point: {points[-1]}")


if __name__ == "__main__":
    main()