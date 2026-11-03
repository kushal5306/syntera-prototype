"""Parametric fitting solids, their placement on a routed line, and their exact checks."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

import cadquery as cq
from pydantic import BaseModel, ConfigDict, Field

from syntera.components.tables import (
    LONG_RADIUS_FACTOR,
    SYNTHETIC_COMPONENT_NOTICE,
    FlangeSize,
    Rating,
    flange_for,
    reducer_for,
    tee_for,
)
from syntera.geometry.cad import exact_shape_distance, obstacle_shape
from syntera.geometry.centreline import ArcSegment, LineSegment, centreline_segments
from syntera.schemas import DemoConfig, FittingSpec, Point3

FITTINGS_FILE = "fittings.json"
Vector3 = tuple[float, float, float]
FittingKind = Literal["elbow_lr", "pipe_bend", "flange", "tee", "reducer"]


def _v(point: Point3 | Vector3) -> cq.Vector:
    return cq.Vector(*point)


def _round3(point: Point3 | Vector3) -> Point3:
    return tuple(round(value, 6) + 0.0 for value in point)  # type: ignore[return-value]


# ---- solids ------------------------------------------------------------------------------


def make_flange(size: FlangeSize, face: Point3, direction: Vector3) -> cq.Shape:
    """Flange whose face sits on ``face`` and whose body runs along ``direction``."""
    axis = _v(direction).normalized()
    disc = cq.Solid.makeCylinder(size.outer_diameter / 2, size.thickness, _v(face), axis)
    bore = cq.Solid.makeCylinder(size.tube_od / 2, size.thickness, _v(face), axis)
    body = disc.cut(bore)
    # bolt holes on a circle in the flange plane
    helper = cq.Vector(0, 0, 1) if abs(axis.z) < 0.9 else cq.Vector(1, 0, 0)
    u = axis.cross(helper).normalized()
    w = axis.cross(u).normalized()
    for index in range(size.bolts):
        angle = 2 * math.pi * (index + 0.5) / size.bolts
        offset = u * (math.cos(angle) * size.bolt_circle / 2) + w * (
            math.sin(angle) * size.bolt_circle / 2
        )
        hole = cq.Solid.makeCylinder(size.bolt_hole / 2, size.thickness, _v(face) + offset, axis)
        body = body.cut(hole)
    return body


def make_elbow(arc: ArcSegment, outer_diameter: float) -> cq.Shape:
    """Solid elbow swept along one exact bend arc."""
    path = cq.Wire.assembleEdges(
        [cq.Edge.makeThreePointArc(_v(arc.start), _v(arc.point_at(0.5)), _v(arc.end))]
    )
    tangent = arc.point_at(1e-6)
    start_direction = cq.Vector(*[tangent[i] - arc.start[i] for i in range(3)]).normalized()
    profile = cq.Wire.makeCircle(outer_diameter / 2, _v(arc.start), start_direction)
    return cq.Solid.sweep(profile, [], path, makeSolid=True, isFrenet=True)


def make_tee(
    run_od: float, branch_od: float, centre: Point3, run_axis: Vector3, branch_axis: Vector3
) -> cq.Shape:
    size = tee_for(run_od, branch_od)
    run = _v(run_axis).normalized()
    start = _v(centre) - run * size.run_centre_to_end
    body = cq.Solid.makeCylinder(run_od / 2, 2 * size.run_centre_to_end, start, run)
    branch = cq.Solid.makeCylinder(
        branch_od / 2, size.branch_centre_to_end, _v(centre), _v(branch_axis).normalized()
    )
    return body.fuse(branch)


def make_reducer(large_od: float, small_od: float, start: Point3, axis: Vector3) -> cq.Shape:
    size = reducer_for(large_od, small_od)
    return cq.Solid.makeCone(
        large_od / 2, small_od / 2, size.length, _v(start), _v(axis).normalized()
    )


# ---- placement ---------------------------------------------------------------------------


class FittingRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: FittingKind
    line: str
    tube_od: float
    rating: Rating | None = None
    position: Point3
    direction: Vector3 | None = None
    radius: float | None = None
    outer_diameter: float
    length_mm: float


class StraightPiece(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line: str
    index: int
    start: Point3
    end: Point3
    length_mm: float


class FittingCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fitting: str
    minimum_clearance_mm: float
    required_clearance_mm: float
    collision_free: bool
    inside_workspace: bool
    passed: bool


class FittingReport(BaseModel):
    """Fittings of one routed line, the straight pipe between them, and their checks."""

    model_config = ConfigDict(extra="forbid")

    line: str
    spec: FittingSpec
    tables: str = SYNTHETIC_COMPONENT_NOTICE
    fittings: list[FittingRecord]
    straight_pieces: list[StraightPiece]
    checks: list[FittingCheck] = Field(default_factory=list)
    overall_pass: bool = True
    failure_reasons: list[str] = Field(default_factory=list)


@dataclass
class FittingSet:
    report: FittingReport
    shapes: dict[str, cq.Shape] = field(default_factory=dict)

    @property
    def flanges(self) -> list[cq.Shape]:
        return [self.shapes[item.id] for item in self.report.fittings if item.kind == "flange"]


def place_fittings(
    points: list[Point3],
    outer_diameter: float,
    bend_radius: float,
    spec: FittingSpec,
    line: str = "main",
    start_direction: Vector3 | None = None,
    end_direction: Vector3 | None = None,
) -> FittingSet:
    """Elbows at every bend, flanges at both ends when flanged, straight pieces between.

    A bend whose radius is the long-radius proportion (1.5 x OD) is a standard long-radius
    elbow; any other radius is a pipe bend made from the tube itself.
    """
    segments = centreline_segments(points, bend_radius)
    records: list[FittingRecord] = []
    shapes: dict[str, cq.Shape] = {}
    pieces: list[StraightPiece] = []
    standard = math.isclose(bend_radius, LONG_RADIUS_FACTOR * outer_diameter, abs_tol=1e-6)
    flange = flange_for(outer_diameter, spec.rating) if spec.end_connection == "flanged" else None
    trim_start = trim_end = flange.thickness if flange else 0.0
    bends = 0
    straight_index = 0
    for number, segment in enumerate(segments):
        if isinstance(segment, ArcSegment):
            bends += 1
            item_id = f"{line}-E{bends}"
            records.append(
                FittingRecord(
                    id=item_id,
                    kind="elbow_lr" if standard else "pipe_bend",
                    line=line,
                    tube_od=outer_diameter,
                    position=_round3(segment.point_at(0.5)),
                    radius=round(segment.radius, 6),
                    outer_diameter=outer_diameter,
                    length_mm=round(segment.length, 6),
                )
            )
            shapes[item_id] = make_elbow(segment, outer_diameter)
            continue
        assert isinstance(segment, LineSegment)
        length = segment.length
        if number == 0:
            length -= trim_start
        if number == len(segments) - 1:
            length -= trim_end
        straight_index += 1
        pieces.append(
            StraightPiece(
                line=line,
                index=straight_index,
                start=_round3(segment.start),
                end=_round3(segment.end),
                length_mm=round(max(length, 0.0), 6),
            )
        )
    if flange is not None:
        first = segments[0]
        last = segments[-1]
        out_start = start_direction or _direction(first.start, first.point_at(1e-6))
        ends = ((points[0], out_start, "F1"),)
        into_end = end_direction or _direction(last.end, last.point_at(1 - 1e-6))
        ends += ((points[-1], into_end, "F2"),)
        for face, direction, suffix in ends:
            item_id = f"{line}-{suffix}"
            records.append(
                FittingRecord(
                    id=item_id,
                    kind="flange",
                    line=line,
                    tube_od=outer_diameter,
                    rating=spec.rating,
                    position=_round3(face),
                    direction=_round3(direction),
                    outer_diameter=flange.outer_diameter,
                    length_mm=flange.thickness,
                )
            )
            shapes[item_id] = make_flange(flange, face, direction)
    records.sort(key=lambda record: record.id)
    report = FittingReport(line=line, spec=spec, fittings=records, straight_pieces=pieces)
    return FittingSet(report=report, shapes=shapes)


def _direction(start: Point3, end: Point3) -> Vector3:
    delta = [end[i] - start[i] for i in range(3)]
    length = math.sqrt(sum(d * d for d in delta))
    return tuple(d / length for d in delta)  # type: ignore[return-value]


# ---- verification ------------------------------------------------------------------------


def verify_fittings(config: DemoConfig, fittings: FittingSet) -> FittingReport:
    """Exact checks for every flange: clearance to obstacles, no collision, in workspace.

    Elbows are the tube's own bends, already measured by the route checks; each one is
    also checked here against the obstacles so the fitting list is fully covered.
    """
    obstacles = [obstacle_shape(obstacle) for obstacle in config.obstacles]
    required = config.tube.minimum_clearance
    dims = config.workspace.dimensions
    checks: list[FittingCheck] = []
    reasons: list[str] = []
    for record in fittings.report.fittings:
        shape = fittings.shapes[record.id]
        box = shape.BoundingBox()
        boundary = min(box.xmin, dims[0] - box.xmax, box.ymin, dims[1] - box.ymax,
                       box.zmin, dims[2] - box.zmax)  # fmt: skip
        distances = [exact_shape_distance(shape, other) for other in obstacles]
        clearance = min([boundary, *distances])
        collision_free = all(shape.intersect(other).Volume() <= 1e-7 for other in obstacles)
        inside = boundary >= -1e-7
        passed = collision_free and inside and clearance + 1e-7 >= required
        checks.append(
            FittingCheck(
                fitting=record.id,
                minimum_clearance_mm=round(clearance, 6),
                required_clearance_mm=required,
                collision_free=collision_free,
                inside_workspace=inside,
                passed=passed,
            )
        )
        if not passed:
            what = "intersects an obstacle" if not collision_free else (
                "leaves the workspace" if not inside else
                f"has {clearance:.3f} mm clearance, below the required {required:.3f} mm"
            )  # fmt: skip
            reasons.append(f"fitting {record.id} ({record.kind}) {what}")
    return fittings.report.model_copy(
        update={"checks": checks, "overall_pass": not reasons, "failure_reasons": reasons}
    )
