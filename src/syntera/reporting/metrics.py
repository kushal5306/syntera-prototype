"""Package size and piping metrics for the KPI 2 comparison (all inputs in millimetres)."""

from __future__ import annotations

import cadquery as cq

from syntera.geometry.centreline import ArcSegment, centreline_segments
from syntera.schemas import DemoConfig, PackageMetrics, PipingMetrics, Point3

MM3_PER_M3 = 1e9
MM2_PER_M2 = 1e6


def piping_metrics(points: list[Point3], bend_radius: float) -> PipingMetrics:
    segments = centreline_segments(points, bend_radius)
    arcs = [segment for segment in segments if isinstance(segment, ArcSegment)]
    straights = [segment for segment in segments if not isinstance(segment, ArcSegment)]
    straight_length = sum(segment.length for segment in straights)
    bend_length = sum(segment.length for segment in arcs)
    return PipingMetrics(
        main_length_mm=round(straight_length + bend_length, 3),
        straight_length_mm=round(straight_length, 3),
        bend_length_mm=round(bend_length, 3),
        straights=len(straights),
        bends=len(arcs),
    )


def package_metrics(
    config: DemoConfig,
    assembly: list[cq.Shape],
    tube: cq.Shape | None,
    route_points: list[Point3],
) -> PackageMetrics:
    """Envelope of equipment, nozzles and (when routed) the tube, from exact B-rep bounds."""
    names = [obstacle.name for obstacle in config.obstacles] + ["start-port", "end-port"]
    shapes = list(assembly)
    if tube is not None:
        shapes.append(tube)
        names.append("routed-tube")
    boxes = [shape.BoundingBox() for shape in shapes]
    low = tuple(round(min(getattr(box, f"{axis}min") for box in boxes), 3) for axis in "xyz")
    high = tuple(round(max(getattr(box, f"{axis}max") for box in boxes), 3) for axis in "xyz")
    size = tuple(round(high[index] - low[index], 3) for index in range(3))
    frame = config.workspace.dimensions
    return PackageMetrics(
        envelope_min_mm=low,
        envelope_max_mm=high,
        envelope_dimensions_mm=size,
        envelope_volume_m3=round(size[0] * size[1] * size[2] / MM3_PER_M3, 9),
        footprint_m2=round(size[0] * size[1] / MM2_PER_M2, 9),
        height_mm=size[2],
        frame_dimensions_mm=frame,
        frame_volume_m3=round(frame[0] * frame[1] * frame[2] / MM3_PER_M3, 9),
        included_parts=names,
        piping=(
            piping_metrics(route_points, config.tube.minimum_bend_radius)
            if tube is not None
            else None
        ),
    )
