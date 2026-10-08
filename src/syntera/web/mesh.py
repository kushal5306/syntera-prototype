"""Convert CadQuery B-reps into browser-friendly triangle meshes."""

from __future__ import annotations

import math
from typing import Any

import cadquery as cq

from syntera.schemas import DemoConfig

Color = tuple[float, float, float]


def _triangle_normal(
    first: tuple[float, float, float],
    second: tuple[float, float, float],
    third: tuple[float, float, float],
) -> tuple[float, float, float]:
    left = tuple(second[index] - first[index] for index in range(3))
    right = tuple(third[index] - first[index] for index in range(3))
    cross = (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )
    length = math.sqrt(sum(value * value for value in cross))
    if length <= 1e-12:
        return (0.0, 0.0, 1.0)
    return tuple(value / length for value in cross)  # type: ignore[return-value]


def shape_mesh(
    identifier: str,
    name: str,
    kind: str,
    shape: cq.Shape,
    color: Color,
    properties: dict[str, Any],
    tolerance: float,
) -> dict[str, Any]:
    """Tessellate one exact shape and expand indexed triangles for simple WebGL."""
    vertices, triangles = shape.tessellate(tolerance)
    source = [vertex.toTuple() for vertex in vertices]
    positions: list[float] = []
    normals: list[float] = []
    for triangle in triangles:
        points = [source[index] for index in triangle]
        normal = _triangle_normal(*points)
        for point in points:
            positions.extend(float(value) for value in point)
            normals.extend(normal)
    box = shape.BoundingBox()
    return {
        "id": identifier,
        "name": name,
        "kind": kind,
        "color": color,
        "positions": positions,
        "normals": normals,
        "triangle_count": len(triangles),
        "bounds": {
            "min": [box.xmin, box.ymin, box.zmin],
            "max": [box.xmax, box.ymax, box.zmax],
        },
        "properties": properties,
    }


OBSTACLE_COLORS: dict[str, Color] = {
    "box": (0.88, 0.34, 0.25),
    "cylinder": (0.95, 0.58, 0.18),
    "step": (0.55, 0.62, 0.72),
}


def scene_meshes(
    config: DemoConfig,
    assembly: list[cq.Shape],
    tube: cq.Shape | None = None,
) -> list[dict[str, Any]]:
    """Create named meshes for obstacles, ports, and an optional routed tube."""
    tolerance = max(0.8, config.voxel_resolution / 8.0)
    meshes: list[dict[str, Any]] = []
    for index, (obstacle, shape) in enumerate(
        zip(config.obstacles, assembly[: len(config.obstacles)], strict=True)
    ):
        color: Color = OBSTACLE_COLORS[obstacle.type]
        meshes.append(
            shape_mesh(
                f"obstacle-{index}",
                obstacle.name,
                "obstacle",
                shape,
                color,
                obstacle.model_dump(mode="json"),
                tolerance,
            )
        )

    port_shapes = assembly[len(config.obstacles) :]
    for label, port, shape, color in (
        ("Start port", config.start_port, port_shapes[0], (0.14, 0.75, 0.55)),
        ("End port", config.end_port, port_shapes[1], (0.78, 0.35, 0.68)),
    ):
        meshes.append(
            shape_mesh(
                label.lower().replace(" ", "-"),
                label,
                "port",
                shape,
                color,
                port.model_dump(mode="json"),
                tolerance,
            )
        )
    if tube is not None:
        meshes.append(
            shape_mesh(
                "routed-tube",
                "Generated tube",
                "route",
                tube,
                (0.08, 0.72, 0.82),
                {
                    "outer_diameter_mm": config.tube.outer_diameter,
                    "bend_radius_mm": config.tube.minimum_bend_radius,
                },
                tolerance,
            )
        )
    return meshes
