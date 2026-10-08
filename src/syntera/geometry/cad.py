"""CadQuery construction and STEP export for synthetic demonstration geometry."""

from __future__ import annotations

from pathlib import Path

import cadquery as cq
from OCP.BRepExtrema import BRepExtrema_DistShapeShape

from syntera.geometry.centreline import LineSegment, centreline_segments, unit_between
from syntera.geometry.step_import import import_step_obstacle
from syntera.schemas import BoxObstacle, DemoConfig, Obstacle, Point3, StepObstacle


def _vector(point: Point3) -> cq.Vector:
    return cq.Vector(*point)


def obstacle_shape(obstacle: Obstacle) -> cq.Shape:
    """Construct an obstacle at its configured position (exact geometry for STEP imports)."""
    if isinstance(obstacle, StepObstacle):
        return import_step_obstacle(obstacle).original
    if isinstance(obstacle, BoxObstacle):
        shape = cq.Solid.makeBox(*obstacle.size)
        offset = tuple(obstacle.center[index] - obstacle.size[index] / 2.0 for index in range(3))
        return shape.translate(cq.Vector(*offset))

    axis = {"x": cq.Vector(1, 0, 0), "y": cq.Vector(0, 1, 0), "z": cq.Vector(0, 0, 1)}[
        obstacle.axis
    ]
    base = _vector(obstacle.center) - axis.multiply(obstacle.height / 2.0)
    return cq.Solid.makeCylinder(obstacle.radius, obstacle.height, base, axis)


def _port_shape(position: Point3, direction: Point3, radius: float) -> cq.Shape:
    direction_vector = _vector(direction)
    base = _vector(position) - direction_vector.multiply(8.0)
    return cq.Solid.makeCylinder(radius * 1.35, 8.0, base, direction_vector)


def assembly_shapes(config: DemoConfig) -> list[cq.Shape]:
    """Return equipment and visibly distinct connection-port solids."""
    tube_radius = config.tube.outer_diameter / 2.0
    return [
        *(obstacle_shape(obstacle) for obstacle in config.obstacles),
        _port_shape(config.start_port.position, config.start_port.direction, tube_radius),
        _port_shape(config.end_port.position, config.end_port.direction, tube_radius),
    ]


def make_tube(points: list[Point3], tube_radius: float, bend_radius: float) -> cq.Shape:
    """Sweep a circular solid along tangent straight lines and exact circular bends."""
    edges: list[cq.Edge] = []
    for segment in centreline_segments(points, bend_radius):
        if isinstance(segment, LineSegment):
            edges.append(cq.Edge.makeLine(_vector(segment.start), _vector(segment.end)))
        else:
            edges.append(
                cq.Edge.makeThreePointArc(
                    _vector(segment.start),
                    _vector(segment.point_at(0.5)),
                    _vector(segment.end),
                )
            )
    path = cq.Wire.assembleEdges(edges)
    initial_direction = unit_between(points[0], points[1])
    profile = cq.Wire.makeCircle(tube_radius, _vector(points[0]), _vector(initial_direction))
    return cq.Solid.sweep(profile, [], path, makeSolid=True, isFrenet=True)


def exact_shape_distance(left: cq.Shape, right: cq.Shape) -> float:
    """Compute exact boundary distance using OpenCascade extrema."""
    calculation = BRepExtrema_DistShapeShape(left.wrapped, right.wrapped)
    calculation.Perform()
    if not calculation.IsDone():
        raise RuntimeError("OpenCascade distance calculation failed")
    return float(calculation.Value())


def export_step(shapes: list[cq.Shape], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    compound = cq.Compound.makeCompound(shapes)
    cq.exporters.export(compound, str(path))
