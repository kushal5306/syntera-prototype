"""Voxel occupancy generated from analytical obstacle geometry."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product

import numpy as np

from syntera.schemas import BoxObstacle, DemoConfig, Obstacle, Point3

Index3 = tuple[int, int, int]


def point_to_obstacle_distance(point: Point3, obstacle: Obstacle) -> float:
    """Return Euclidean distance from a point to an obstacle solid (zero inside)."""
    p = np.asarray(point, dtype=float)
    center = np.asarray(obstacle.center, dtype=float)
    if isinstance(obstacle, BoxObstacle):
        delta = np.maximum(np.abs(p - center) - np.asarray(obstacle.size) / 2.0, 0.0)
        return float(np.linalg.norm(delta))

    axis_index = {"x": 0, "y": 1, "z": 2}[obstacle.axis]
    radial_indices = [index for index in range(3) if index != axis_index]
    radial = np.linalg.norm((p - center)[radial_indices])
    axial = abs(p[axis_index] - center[axis_index])
    delta = np.maximum([radial - obstacle.radius, axial - obstacle.height / 2.0], 0.0)
    return float(np.linalg.norm(delta))


@dataclass(frozen=True)
class OccupancyGrid:
    """Regular grid whose nodes represent possible tube centreline locations."""

    occupied: np.ndarray
    axes: tuple[np.ndarray, np.ndarray, np.ndarray]
    resolution: float
    inflation_radius: float
    obstacles: tuple[Obstacle, ...]

    @classmethod
    def from_config(cls, config: DemoConfig) -> OccupancyGrid:
        resolution = config.voxel_resolution
        axes = tuple(
            np.arange(0.0, dimension + resolution * 0.25, resolution, dtype=float)
            for dimension in config.workspace.dimensions
        )
        shape = tuple(len(axis) for axis in axes)
        occupied = np.zeros(shape, dtype=bool)
        inflation = config.tube.outer_diameter / 2.0 + config.tube.minimum_clearance

        for index in product(*(range(size) for size in shape)):
            point = tuple(float(axes[axis][index[axis]]) for axis in range(3))
            boundary_clearance = min(
                min(coordinate, dimension - coordinate)
                for coordinate, dimension in zip(point, config.workspace.dimensions, strict=False)
            )
            occupied[index] = boundary_clearance + 1e-9 < inflation or any(
                point_to_obstacle_distance(point, obstacle) + 1e-9 < inflation
                for obstacle in config.obstacles
            )
        return cls(occupied, axes, resolution, inflation, tuple(config.obstacles))

    @property
    def shape(self) -> tuple[int, int, int]:
        return self.occupied.shape

    def point(self, index: Index3) -> Point3:
        return tuple(float(self.axes[axis][index[axis]]) for axis in range(3))  # type: ignore[return-value]

    def index(self, point: Point3) -> Index3:
        values = tuple(int(round(point[axis] / self.resolution)) for axis in range(3))
        if any(index < 0 or index >= self.shape[axis] for axis, index in enumerate(values)):
            raise ValueError(f"point {point} is outside the occupancy grid")
        snapped = self.point(values)
        if any(
            abs(actual - expected) > 1e-6 for actual, expected in zip(snapped, point, strict=False)
        ):
            raise ValueError(f"port {point} must align to the voxel resolution")
        return values

    def centreline_clearance(self, index: Index3) -> float:
        point = self.point(index)
        obstacle_distance = min(
            (point_to_obstacle_distance(point, obstacle) for obstacle in self.obstacles),
            default=float("inf"),
        )
        boundary_distance = min(
            min(point[axis], self.axes[axis][-1] - point[axis]) for axis in range(3)
        )
        return min(obstacle_distance, boundary_distance)
