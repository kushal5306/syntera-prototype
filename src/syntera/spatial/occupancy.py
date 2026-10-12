"""Voxel occupancy generated from analytical obstacle geometry."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from syntera.geometry.step_import import import_step_obstacle, point_to_imported_distance
from syntera.schemas import BoxObstacle, DemoConfig, Obstacle, Point3, StepObstacle

Index3 = tuple[int, int, int]


def point_to_obstacle_distance(point: Point3, obstacle: Obstacle) -> float:
    """Return Euclidean distance from a point to an obstacle solid (zero inside).

    STEP obstacles are measured against their conservative routing solid, which contains
    the imported geometry, so the result never exceeds the true distance.
    """
    if isinstance(obstacle, StepObstacle):
        return point_to_imported_distance(point, import_step_obstacle(obstacle))
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


def _distance_field(grid: np.ndarray, obstacle: Obstacle) -> np.ndarray:
    """Vectorised :func:`point_to_obstacle_distance` over every grid node."""
    if isinstance(obstacle, StepObstacle):
        flat = grid.reshape(-1, 3)
        values = [point_to_obstacle_distance(tuple(map(float, p)), obstacle) for p in flat]
        return np.asarray(values, dtype=float).reshape(grid.shape[:-1])
    center = np.asarray(obstacle.center, dtype=float)
    delta = grid - center
    if isinstance(obstacle, BoxObstacle):
        outside = np.maximum(np.abs(delta) - np.asarray(obstacle.size) / 2.0, 0.0)
        return np.linalg.norm(outside, axis=-1)
    axis_index = {"x": 0, "y": 1, "z": 2}[obstacle.axis]
    radial_indices = [index for index in range(3) if index != axis_index]
    radial = np.linalg.norm(delta[..., radial_indices], axis=-1)
    axial = np.abs(delta[..., axis_index])
    outside = np.stack(
        [np.maximum(radial - obstacle.radius, 0.0), np.maximum(axial - obstacle.height / 2.0, 0.0)],
        axis=-1,
    )
    return np.linalg.norm(outside, axis=-1)


@dataclass(frozen=True)
class OccupancyGrid:
    """Regular grid whose nodes represent possible tube centreline locations."""

    occupied: np.ndarray
    axes: tuple[np.ndarray, np.ndarray, np.ndarray]
    resolution: float
    inflation_radius: float
    obstacle_distance: np.ndarray

    @classmethod
    def from_config(cls, config: DemoConfig) -> OccupancyGrid:
        resolution = config.voxel_resolution
        axes = tuple(
            np.arange(0.0, dimension + resolution * 0.25, resolution, dtype=float)
            for dimension in config.workspace.dimensions
        )
        shape = tuple(len(axis) for axis in axes)
        inflation = config.tube.outer_diameter / 2.0 + config.tube.minimum_clearance
        grid = np.stack(np.meshgrid(*axes, indexing="ij"), axis=-1)
        dims = np.asarray(config.workspace.dimensions, dtype=float)
        boundary_clearance = np.minimum(grid, dims - grid).min(axis=-1)
        obstacle_distance = np.full(shape, np.inf, dtype=float)
        for obstacle in config.obstacles:
            obstacle_distance = np.minimum(obstacle_distance, _distance_field(grid, obstacle))
        occupied = (boundary_clearance + 1e-9 < inflation) | (obstacle_distance + 1e-9 < inflation)
        return cls(occupied, axes, resolution, inflation, obstacle_distance)

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
        boundary_distance = min(
            min(point[axis], self.axes[axis][-1] - point[axis]) for axis in range(3)
        )
        return min(float(self.obstacle_distance[index]), boundary_distance)
