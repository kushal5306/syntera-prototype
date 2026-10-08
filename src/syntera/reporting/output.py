"""JSON and preview output helpers."""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from mpl_toolkits.mplot3d.art3d import Poly3DCollection  # noqa: E402
from pydantic import BaseModel

from syntera.schemas import BoxObstacle, DemoConfig, RouteResult


def write_json(model: BaseModel, path: Path) -> None:
    path.write_text(
        json.dumps(model.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _box_faces(center: tuple[float, ...], size: tuple[float, ...]) -> list[list[tuple[float, ...]]]:
    lower = tuple(center[i] - size[i] / 2 for i in range(3))
    upper = tuple(center[i] + size[i] / 2 for i in range(3))
    vertices = [
        (x, y, z)
        for x in (lower[0], upper[0])
        for y in (lower[1], upper[1])
        for z in (lower[2], upper[2])
    ]
    indices = (
        (0, 1, 3, 2),
        (4, 5, 7, 6),
        (0, 1, 5, 4),
        (2, 3, 7, 6),
        (0, 2, 6, 4),
        (1, 3, 7, 5),
    )
    return [[vertices[index] for index in face] for face in indices]


def write_preview(config: DemoConfig, route: RouteResult, path: Path) -> None:
    """Write a diagnostic 3D preview; it is not an assurance check."""
    figure = plt.figure(figsize=(9, 6))
    axes = figure.add_subplot(111, projection="3d")
    for obstacle in config.obstacles:
        if isinstance(obstacle, BoxObstacle):
            collection = Poly3DCollection(
                _box_faces(obstacle.center, obstacle.size),
                alpha=0.28,
                facecolor="#d95f59",
            )
            axes.add_collection3d(collection)
        else:
            axes.scatter(*obstacle.center, s=180, c="#d95f59", marker="s", alpha=0.5)
    if route.found:
        x, y, z = zip(*route.points, strict=False)
        axes.plot(x, y, z, color="#0072b2", linewidth=3, marker="o")
    axes.scatter(*config.start_port.position, c="#009e73", s=55, label="start")
    axes.scatter(*config.end_port.position, c="#cc79a7", s=55, label="end")
    axes.set(xlabel="X (mm)", ylabel="Y (mm)", zlabel="Z (mm)")
    axes.set_xlim(0, config.workspace.dimensions[0])
    axes.set_ylim(0, config.workspace.dimensions[1])
    axes.set_zlim(0, config.workspace.dimensions[2])
    axes.set_title("Syntera synthetic route preview")
    axes.legend()
    figure.tight_layout()
    figure.savefig(path, dpi=150)
    plt.close(figure)
