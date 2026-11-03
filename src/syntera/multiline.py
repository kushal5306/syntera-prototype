"""Route every line of a design document, largest bore first (roadmap B1).

Lines are routed one at a time in a deterministic order: largest outside diameter first,
then by line id. Each line is routed and verified by the ordinary single-line pipeline
with all equipment, conservative boxes around every pipe already routed, and reserved
space at the nozzles of lines not yet routed as obstacles. After all lines are routed,
every pair of tube solids is checked with exact geometry (no intersection, and a gap of
at least the larger of the two clearances).

The overall verdict is plain logic over these deterministic checks; nothing here can be
set from outside, and a line that fails is never traded against a shorter design.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path

import cadquery as cq
from pydantic import BaseModel, ConfigDict

from syntera.design import (
    DESIGN_FILE,
    DesignDocument,
    DesignError,
    Line,
    Nozzle,
    run_config,
    run_rationale,
    save_design,
    with_route,
)
from syntera.geometry.cad import assembly_shapes, exact_shape_distance, export_step
from syntera.pipeline import PipelineResult, execute_pipeline
from syntera.rationale import Rationale
from syntera.reporting.output import write_json
from syntera.schemas import BoxObstacle, DemoConfig, Point3, TubeSpecification

DESIGN_REPORT_FILE = "design_report.json"
ROUTED_DESIGN_STEP = "routed_design.step"
LINES_DIR = "lines"
ORDER_RULE = "design.largest_bore_first.v1"
INTERSECTION_TOLERANCE_MM3 = 1e-7


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LineOutcome(StrictModel):
    """What the single-line pipeline found for one line of the design."""

    line: str
    order: int
    outer_diameter_mm: float
    routed: bool
    overall_pass: bool
    route_length_mm: float | None
    bends: int
    obstacles: int
    output: str
    failure_reasons: list[str]


class PairCheck(StrictModel):
    """Exact clearance between two routed tube solids."""

    lines: tuple[str, str]
    minimum_distance_mm: float
    required_clearance_mm: float
    intersects: bool
    passed: bool


class DesignRunReport(StrictModel):
    """Deterministic result of routing every line of a design document."""

    title: str
    synthetic_data: bool
    order_rule: str
    line_order: list[str]
    lines: list[LineOutcome]
    pairs: list[PairCheck]
    overall_pass: bool
    failure_reasons: list[str]


@dataclass(frozen=True)
class DesignRunResult:
    design: DesignDocument
    report: DesignRunReport
    lines: dict[str, PipelineResult]


def routing_order(design: DesignDocument) -> list[Line]:
    """Largest outside diameter first; equal sizes in line-id order."""
    return sorted(design.lines, key=lambda line: (-line.tube.outer_diameter, line.id))


def check_nozzles_unshared(design: DesignDocument) -> None:
    """Each nozzle serves one line; branch connections (tees) are not routed here."""
    users: dict[str, list[str]] = {}
    for line in design.lines:
        for nozzle in (line.from_nozzle, line.to_nozzle):
            users.setdefault(nozzle, []).append(line.id)
    shared = {nozzle: ids for nozzle, ids in sorted(users.items()) if len(ids) > 1}
    if shared:
        detail = "; ".join(f"{nozzle}: {', '.join(ids)}" for nozzle, ids in shared.items())
        raise DesignError(
            f"nozzles shared by several lines need a tee, which is not supported yet ({detail})"
        )


def _box(name: str, low: list[float], high: list[float]) -> BoxObstacle:
    return BoxObstacle(
        type="box",
        name=name,
        center=tuple((low[i] + high[i]) / 2 for i in range(3)),  # type: ignore[arg-type]
        size=tuple(max(high[i] - low[i], 1e-3) for i in range(3)),  # type: ignore[arg-type]
    )


def pipe_obstacles(
    line_id: str, points: list[Point3], tube: TubeSpecification, margin: float = 0.0
) -> list[BoxObstacle]:
    """Conservative boxes that contain a routed tube: one per leg and one per bend.

    Each leg box covers the straight run at the outside radius; each bend box covers the
    arc, which stays within one bend radius of its corner. ``margin`` grows every box so
    a later line keeps a clearance larger than its own when this line needs one.
    """
    half = tube.outer_diameter / 2 + margin
    radius = tube.minimum_bend_radius
    boxes: list[BoxObstacle] = []
    for index, (a, b) in enumerate(zip(points, points[1:], strict=False)):
        low = [min(a[i], b[i]) - half for i in range(3)]
        high = [max(a[i], b[i]) + half for i in range(3)]
        boxes.append(_box(f"line {line_id} leg {index + 1}", low, high))
    for index, corner in enumerate(points[1:-1], start=1):
        low = [corner[i] - radius - half for i in range(3)]
        high = [corner[i] + radius + half for i in range(3)]
        boxes.append(_box(f"line {line_id} bend {index}", low, high))
    return boxes


def nozzle_reserve(
    line_id: str, nozzle: Nozzle, tube: TubeSpecification, workspace: tuple[float, ...]
) -> BoxObstacle:
    """Space kept free at a nozzle of a line not yet routed.

    The box runs from the port along its direction for one bend radius plus one outside
    diameter (the straight run before the first bend), with half-width of the outside
    radius plus the line's clearance, clipped to the workspace.
    """
    position, direction = nozzle.port.position, nozzle.port.direction
    half = tube.outer_diameter / 2 + tube.minimum_clearance
    reach = tube.minimum_bend_radius + tube.outer_diameter
    far = [position[i] + direction[i] * reach for i in range(3)]
    low, high = [], []
    for i in range(3):
        if direction[i] != 0:
            lo, hi = sorted((position[i], far[i]))
        else:
            lo, hi = position[i] - half, position[i] + half
        low.append(min(max(lo, 0.0), workspace[i]))
        high.append(min(max(hi, 0.0), workspace[i]))
    return _box(f"line {line_id} nozzle {nozzle.id} reserve", low, high)


def line_config(
    design: DesignDocument,
    line: Line,
    routed: Iterable[tuple[Line, list[Point3]]],
    pending: Iterable[Line],
) -> DemoConfig:
    """Run configuration for ``line`` with earlier pipes and pending nozzles as obstacles."""
    base = run_config(design, line.id)
    extra: list[BoxObstacle] = []
    for other, points in routed:
        margin = max(0.0, other.tube.minimum_clearance - line.tube.minimum_clearance)
        extra.extend(pipe_obstacles(other.id, points, other.tube, margin))
    for other in pending:
        for nozzle_id in (other.from_nozzle, other.to_nozzle):
            extra.append(
                nozzle_reserve(
                    other.id, design.nozzle(nozzle_id), other.tube, design.workspace.dimensions
                )
            )
    data = base.model_dump(mode="json")
    data["obstacles"] = [*data["obstacles"], *(box.model_dump(mode="json") for box in extra)]
    return DemoConfig.model_validate(data)


def order_rationale(line: Line, order: int, total: int) -> Rationale:
    return Rationale(
        path="routing_order",
        value=order,
        source="rule",
        rule_id=ORDER_RULE,
        inputs={"outer_diameter_mm": line.tube.outer_diameter, "lines": total},
        explanation=(
            "line routed {value} of {lines}: largest outside diameter first "
            "({outer_diameter_mm} mm), equal sizes in line-id order, so larger pipes get "
            "the most direct runs and smaller ones route around them."
        ),
    )


def pair_check(a: Line, b: Line, tube_a: cq.Shape, tube_b: cq.Shape) -> PairCheck:
    """Exact geometry check between two routed tubes."""
    required = max(a.tube.minimum_clearance, b.tube.minimum_clearance)
    distance = exact_shape_distance(tube_a, tube_b)
    intersects = tube_a.intersect(tube_b).Volume() > INTERSECTION_TOLERANCE_MM3
    first, second = sorted((a.id, b.id))
    return PairCheck(
        lines=(first, second),
        minimum_distance_mm=distance,
        required_clearance_mm=required,
        intersects=intersects,
        passed=not intersects and distance + 1e-7 >= required,
    )


def route_design(design: DesignDocument, output: Path) -> DesignRunResult:
    """Route and verify every line; write per-line outputs, the design and its report."""
    check_nozzles_unshared(design)
    order = routing_order(design)
    output.mkdir(parents=True, exist_ok=True)
    for name in (DESIGN_FILE, DESIGN_REPORT_FILE, ROUTED_DESIGN_STEP):
        (output / name).unlink(missing_ok=True)

    routed: list[tuple[Line, list[Point3]]] = []
    results: dict[str, PipelineResult] = {}
    outcomes: list[LineOutcome] = []
    updated = design
    for index, line in enumerate(order, start=1):
        config = line_config(design, line, routed, order[index:])
        records = [*run_rationale(design, line.id), order_rationale(line, index, len(order))]
        folder = output / LINES_DIR / line.id
        result = execute_pipeline(config, folder, create_preview=False, rationale=records)
        results[line.id] = result
        updated = with_route(updated, line.id, result.route)
        report = result.report
        if result.route.found and result.tube is not None:
            routed.append((line, result.route.points))
        outcomes.append(
            LineOutcome(
                line=line.id,
                order=index,
                outer_diameter_mm=line.tube.outer_diameter,
                routed=report.route_found,
                overall_pass=report.overall_pass,
                route_length_mm=report.route_length_mm,
                bends=report.number_of_bends,
                obstacles=len(config.obstacles),
                output=f"{LINES_DIR}/{line.id}",
                failure_reasons=report.failure_reasons,
            )
        )

    by_id = {line.id: line for line in design.lines}
    tubes = {line_id: result.tube for line_id, result in sorted(results.items()) if result.tube}
    pairs = [pair_check(by_id[a], by_id[b], tubes[a], tubes[b]) for a, b in combinations(tubes, 2)]
    reasons = [
        f"line {outcome.line}: {reason}"
        for outcome in outcomes
        for reason in (outcome.failure_reasons or ([] if outcome.overall_pass else ["failed"]))
    ]
    for pair in pairs:
        if not pair.passed:
            a, b = pair.lines
            what = "intersect" if pair.intersects else "are too close"
            reasons.append(
                f"lines {a} and {b} {what}: {pair.minimum_distance_mm:.3f} mm measured, "
                f"{pair.required_clearance_mm:.3f} mm required"
            )
    report = DesignRunReport(
        title=design.title,
        synthetic_data=design.synthetic_data,
        order_rule=ORDER_RULE,
        line_order=[line.id for line in order],
        lines=outcomes,
        pairs=pairs,
        overall_pass=not reasons,
        failure_reasons=reasons,
    )
    save_design(updated, output / DESIGN_FILE)
    write_json(report, output / DESIGN_REPORT_FILE)
    equipment = assembly_shapes(run_config(design, order[0].id))[: len(design.equipment)]
    export_step([*equipment, *tubes.values()], output / ROUTED_DESIGN_STEP)
    return DesignRunResult(design=updated, report=report, lines=results)
