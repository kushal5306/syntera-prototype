"""CadQuery construction and STEP export for synthetic demonstration geometry."""

from __future__ import annotations

import math
from pathlib import Path

import cadquery as cq
from OCP.BRepExtrema import BRepExtrema_DistShapeShape

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


def _unit_between(start: Point3, end: Point3) -> tuple[float, float, float]:
    length = math.dist(start, end)
    if length <= 1e-9:
        raise ValueError("route contains repeated points")
    return tuple((end[index] - start[index]) / length for index in range(3))  # type: ignore[return-value]


def _offset(point: Point3, direction: Point3, distance: float) -> Point3:
    return tuple(point[index] + direction[index] * distance for index in range(3))  # type: ignore[return-value]


def make_tube(points: list[Point3], tube_radius: float, bend_radius: float) -> cq.Shape:
    """Sweep a circular solid along tangent straight lines and exact circular bends."""
    if len(points) < 2:
        raise ValueError("a tube route requires at least two points")
    edges: list[cq.Edge] = []
    current = points[0]
    for index in range(1, len(points) - 1):
        corner = points[index]
        incoming = _unit_between(points[index - 1], corner)
        outgoing = _unit_between(corner, points[index + 1])
        dot = sum(incoming[axis] * outgoing[axis] for axis in range(3))
        if abs(dot) > 1e-9:
            raise ValueError("route contains a non-orthogonal or collinear corner")
        tangent_in = _offset(corner, incoming, -bend_radius)
        tangent_out = _offset(corner, outgoing, bend_radius)
        if math.dist(current, tangent_in) > 1e-7:
            edges.append(cq.Edge.makeLine(_vector(current), _vector(tangent_in)))
        center = _offset(_offset(corner, incoming, -bend_radius), outgoing, bend_radius)
        radial_in = tuple(tangent_in[axis] - center[axis] for axis in range(3))
        radial_out = tuple(tangent_out[axis] - center[axis] for axis in range(3))
        midpoint = tuple(
            center[axis] + (radial_in[axis] + radial_out[axis]) / math.sqrt(2.0)
            for axis in range(3)
        )
        edges.append(
            cq.Edge.makeThreePointArc(_vector(tangent_in), _vector(midpoint), _vector(tangent_out))
        )
        current = tangent_out
    if math.dist(current, points[-1]) > 1e-7:
        edges.append(cq.Edge.makeLine(_vector(current), _vector(points[-1])))
    path = cq.Wire.assembleEdges(edges)
    initial_direction = _unit_between(points[0], points[1])
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
