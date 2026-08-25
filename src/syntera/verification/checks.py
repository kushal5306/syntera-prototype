"""Deterministic CAD-level route assurance checks."""

from __future__ import annotations

import math
import time

import cadquery as cq

from syntera.geometry.cad import exact_shape_distance, obstacle_shape
from syntera.schemas import AssuranceReport, DemoConfig, Point3, RouteResult


def _unit(start: Point3, end: Point3) -> Point3:
    length = math.dist(start, end)
    return tuple((end[index] - start[index]) / length for index in range(3))  # type: ignore[return-value]


def _aligned(actual: Point3, expected: Point3, tolerance: float = 1e-8) -> bool:
    return sum(actual[index] * expected[index] for index in range(3)) >= 1.0 - tolerance


def _bend_count(points: list[Point3]) -> int:
    return max(0, len(points) - 2)


def failed_report(config: DemoConfig, route: RouteResult, elapsed: float) -> AssuranceReport:
    return AssuranceReport(
        route_found=False,
        route_length_mm=None,
        number_of_bends=0,
        collision_free=False,
        minimum_measured_clearance_mm=None,
        required_clearance_mm=config.tube.minimum_clearance,
        minimum_generated_bend_radius_mm=None,
        required_bend_radius_mm=config.tube.minimum_bend_radius,
        start_port_alignment=False,
        end_port_alignment=False,
        execution_time_seconds=elapsed,
        overall_pass=False,
        failure_reasons=[route.diagnostic or "route generation failed"],
    )


def verify_route(
    config: DemoConfig, route: RouteResult, tube: cq.Shape, started_at: float
) -> AssuranceReport:
    """Verify the generated solid independently of grid occupancy."""
    obstacle_shapes = [obstacle_shape(obstacle) for obstacle in config.obstacles]
    distances = [exact_shape_distance(tube, shape) for shape in obstacle_shapes]
    box = tube.BoundingBox()
    dimensions = config.workspace.dimensions
    boundary_clearance = min(
        box.xmin,
        dimensions[0] - box.xmax,
        box.ymin,
        dimensions[1] - box.ymax,
        box.zmin,
        dimensions[2] - box.zmax,
    )
    minimum_clearance = min([boundary_clearance, *distances])
    collision_free = boundary_clearance >= -1e-7 and all(
        tube.intersect(shape).Volume() <= 1e-7 for shape in obstacle_shapes
    )
    start_alignment = _aligned(_unit(route.points[0], route.points[1]), config.start_port.direction)
    approach = _unit(route.points[-2], route.points[-1])
    end_expected = tuple(-value for value in config.end_port.direction)
    end_alignment = _aligned(approach, end_expected)  # type: ignore[arg-type]
    generated_radius = config.tube.minimum_bend_radius if _bend_count(route.points) else None

    reasons: list[str] = []
    if not collision_free:
        reasons.append("exact CAD geometry intersects an obstacle or workspace boundary")
    if minimum_clearance + 1e-7 < config.tube.minimum_clearance:
        reasons.append(
            f"measured clearance {minimum_clearance:.3f} mm is below required "
            f"{config.tube.minimum_clearance:.3f} mm"
        )
    if generated_radius is not None and generated_radius + 1e-7 < config.tube.minimum_bend_radius:
        reasons.append("generated bend radius is below the specified minimum")
    if not start_alignment:
        reasons.append("tube is not aligned with the start port direction")
    if not end_alignment:
        reasons.append("tube is not aligned with the end port direction")

    return AssuranceReport(
        route_found=True,
        route_length_mm=route.length,
        number_of_bends=_bend_count(route.points),
        collision_free=collision_free,
        minimum_measured_clearance_mm=minimum_clearance,
        required_clearance_mm=config.tube.minimum_clearance,
        minimum_generated_bend_radius_mm=generated_radius,
        required_bend_radius_mm=config.tube.minimum_bend_radius,
        start_port_alignment=start_alignment,
        end_port_alignment=end_alignment,
        execution_time_seconds=time.perf_counter() - started_at,
        overall_pass=not reasons,
        failure_reasons=reasons,
    )
