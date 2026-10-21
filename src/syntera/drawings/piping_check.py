"""Build a 3D assembly from a plan drawing and check its piping as drawn against Syntera's.

The drawn route and Syntera's route are both swept into exact CAD tubes and verified with
the same deterministic checks (collision, clearance, bend radius, port alignment). A saving
is only claimed for a Syntera route that passes every check; hard constraints are never
traded for length.
"""

from __future__ import annotations

import json
import math
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

import cadquery as cq
from ezdxf.document import Drawing

from syntera.drawings.layout3d import DrawnLine, InferredLayout, LineEnd, infer_layout
from syntera.drawings.models import (
    AssemblyOptions,
    DrawingSummary,
    LineCheck,
    PipingCheckReport,
    RouteCheck,
)
from syntera.geometry.cad import (
    assembly_shapes,
    exact_shape_distance,
    export_step,
    make_tube,
    obstacle_shape,
)
from syntera.pipeline import execute_pipeline
from syntera.reporting.metrics import piping_metrics
from syntera.schemas import (
    AssuranceReport,
    BoxObstacle,
    CylinderObstacle,
    DemoConfig,
    Point3,
    Port,
    RouteResult,
    RoutingWeights,
    TubeSpecification,
    Workspace,
)
from syntera.verification.assembly_audit import AssemblyAudit, AuditPart, PipeEnd, audit_assembly
from syntera.verification.checks import verify_route

ASSEMBLY_DIR = "assembly"
REPORT_FILE = "piping_check.json"
CONFIG_FILE = "config.json"
DRAWN_STEP = "drawn_assembly.step"
PLANT_STEP = "drawn_plant.step"  # every drawn line, nozzle, equipment item and column
ASSEMBLY_FILES = {REPORT_FILE: "application/json", PLANT_STEP: "application/step"}
OTHER_LINE = "drawn line "  # obstacle names for other lines' legs in a line's routing
NOT_REQUESTED = "not requested: checked as drawn only"
LINE_FILES = {
    CONFIG_FILE: "application/json",
    DRAWN_STEP: "application/step",
    "assembly.step": "application/step",
    "routed_assembly.step": "application/step",
    "route.json": "application/json",
    "assurance_report.json": "application/json",
    "metrics.json": "application/json",
    "layout.dxf": "application/dxf",
}
VOXEL_STEPS_MM = (5.0, 10.0, 20.0, 25.0, 50.0, 100.0, 200.0, 250.0, 500.0)
MAX_VOXELS = 64_000
SIGNIFICANT_SAVING_PERCENT = 0.5


@dataclass(frozen=True)
class LineGeometry:
    """Exact shapes for one checked line, for the 3D view."""

    config: DemoConfig
    assembly: list[cq.Shape]
    drawn_tube: cq.Shape | None
    optimised_tube: cq.Shape | None
    stubs: tuple[cq.Shape, ...] = ()  # nozzle stubs from the equipment to the drawn ports


@dataclass(frozen=True)
class PipingCheckResult:
    report: PipingCheckReport
    lines: list[LineGeometry]


def voxel_resolution(dimensions: Point3, max_voxels: int = MAX_VOXELS) -> float:
    """Finest standard routing grid that keeps the occupancy grid small enough to search."""
    for step in VOXEL_STEPS_MM:
        count = math.prod(math.floor(size / step) + 1 for size in dimensions)
        if count <= max_voxels:
            return step
    return VOXEL_STEPS_MM[-1]


def _direction(a: Point3, b: Point3) -> Point3:
    length = math.dist(a, b)
    return tuple(round((b[i] - a[i]) / length, 9) for i in range(3))  # type: ignore[return-value]


def _config(
    layout: InferredLayout,
    title: str,
    start: Point3,
    start_direction: Point3,
    end: Point3,
    end_direction: Point3,
    resolution: float,
    tube: TubeSpecification | None = None,
    extra: list[BoxObstacle | CylinderObstacle] | None = None,
) -> DemoConfig:
    assert layout.workspace is not None and layout.tube is not None
    return DemoConfig(
        title=title,
        synthetic_data=True,
        workspace=Workspace(units="mm", dimensions=layout.workspace),
        obstacles=[*layout.obstacles, *(extra or [])],
        start_port=Port(position=start, direction=start_direction),
        end_port=Port(position=end, direction=end_direction),
        tube=tube or layout.tube,
        voxel_resolution=resolution,
        routing_cost_weights=RoutingWeights(
            bend=4 * resolution, obstacle_proximity=2 * resolution, port_direction=1000
        ),
    )


def _snap(
    point: Point3, resolution: float, dimensions: Point3, outward: Point3 | None = None
) -> Point3:
    """Nearest grid point; along a port's axis, the next one outward so it stays clear."""
    snapped = []
    for i in range(3):
        step = point[i] / resolution
        if outward is not None and abs(outward[i]) > 0.5:
            value = math.ceil(step - 1e-9) if outward[i] > 0 else math.floor(step + 1e-9)
        else:
            value = round(step)
        top = math.floor(dimensions[i] / resolution) * resolution
        snapped.append(min(max(value * resolution, 0.0), top))
    return tuple(snapped)  # type: ignore[return-value]


def bend_space_problems(points: list[Point3], bend_radius: float) -> list[str]:
    """Straights must be long enough for the bends at their ends (R per bend)."""
    problems = []
    last = len(points) - 2
    for index, (a, b) in enumerate(zip(points, points[1:], strict=False)):
        needed = bend_radius * ((index > 0) + (index < last))
        length = math.dist(a, b)
        if length + 1e-6 < needed:
            problems.append(
                f"leg {index + 1} is {length:.0f} mm long but its bends need {needed:.0f} mm"
            )
    return problems


def _cost(length_mm: float | None, bends: int, options: AssemblyOptions) -> float | None:
    if length_mm is None:
        return None
    return round(length_mm / 1000 * options.cost_per_metre + bends * options.cost_per_bend, 2)


def _route_check(
    points: list[Point3],
    bend_radius: float,
    options: AssemblyOptions,
    report: AssuranceReport | None,
    reasons: list[str],
    buildable: bool,
) -> RouteCheck:
    bends = max(0, len(points) - 2)
    straight = bend = length = None
    if points and buildable:
        metrics = piping_metrics(points, bend_radius)
        straight, bend, length = (
            metrics.straight_length_mm,
            metrics.bend_length_mm,
            metrics.main_length_mm,
        )
    elif len(points) >= 2:
        length = round(sum(math.dist(a, b) for a, b in zip(points, points[1:], strict=False)), 3)
    failures = list(reasons) + (report.failure_reasons if report else [])
    return RouteCheck(
        points=points,
        length_mm=length,
        straight_length_mm=straight,
        bend_length_mm=bend,
        bends=bends,
        estimated_cost=_cost(length, bends, options),
        buildable=buildable,
        assurance_pass=bool(report and report.overall_pass and not reasons),
        collision_free=report.collision_free if report else None,
        minimum_clearance_mm=(
            round(report.minimum_measured_clearance_mm, 3)
            if report and report.minimum_measured_clearance_mm is not None
            else None
        ),
        failure_reasons=failures,
    )


def _check_drawn(
    line: DrawnLine, config: DemoConfig, options: AssemblyOptions
) -> tuple[RouteCheck, cq.Shape | None]:
    radius = config.tube.minimum_bend_radius
    if line.problems:
        reasons = [f"the line {problem}" for problem in line.problems]
        return _route_check(line.points, radius, options, None, reasons, False), None
    if not line.orthogonal:
        reason = "the drawn run has angled legs; only square (90 degree) bends are checked"
        return _route_check(line.points, radius, options, None, [reason], False), None
    problems = bend_space_problems(line.points, radius)
    if problems:
        reasons = [f"cannot be built with a {radius:g} mm bend radius: " + "; ".join(problems)]
        return _route_check(line.points, radius, options, None, reasons, False), None
    started = time.perf_counter()
    try:
        tube = make_tube(line.points, config.tube.outer_diameter / 2, radius)
    except (RuntimeError, ValueError) as error:
        reason = f"CAD tube construction failed: {error}"
        return _route_check(line.points, radius, options, None, [reason], False), None
    route = RouteResult(found=True, points=line.points, length=0.0)
    report = verify_route(config, route, tube, started)
    check = _route_check(line.points, radius, options, report, [], True)
    return check.model_copy(update={"too_close_to": _too_close(config, tube)}), tube


def _too_close(config: DemoConfig, tube: cq.Shape) -> list[str]:
    """Equipment the tube intersects or passes within the required clearance of."""
    names = []
    for obstacle in config.obstacles:
        distance = exact_shape_distance(tube, obstacle_shape(obstacle))
        if distance + 1e-7 < config.tube.minimum_clearance:
            names.append(obstacle.name)
    return names


def _findings(
    name: str,
    drawn: RouteCheck,
    optimised: RouteCheck,
    saving: float | None,
    percent: float | None,
    bend_saving: int | None,
    cost: float | None,
    currency: str,
    required_clearance: float,
) -> list[str]:
    found: list[str] = []
    if not drawn.buildable:
        found.append(f"{name} as drawn: " + "; ".join(drawn.failure_reasons) + ".")
    elif drawn.collision_free is False:
        where = f" ({', '.join(drawn.too_close_to)})" if drawn.too_close_to else ""
        found.append(f"{name} as drawn clashes with equipment or the skid limits{where}.")
    elif drawn.minimum_clearance_mm is not None and (
        drawn.minimum_clearance_mm + 1e-6 < required_clearance
    ):
        found.append(
            f"{name} as drawn passes {drawn.minimum_clearance_mm:.0f} mm from "
            f"{', '.join(drawn.too_close_to) or 'the skid edge'}; "
            f"{required_clearance:g} mm is required."
        )
    elif not drawn.assurance_pass:
        found.append(f"{name} as drawn: " + "; ".join(drawn.failure_reasons) + ".")
    if optimised.failure_reasons == [NOT_REQUESTED]:
        return found
    if not optimised.assurance_pass:
        reasons = "; ".join(optimised.failure_reasons) or "no route found"
        found.append(
            f"{name}: Syntera found no verified alternative ({reasons}). No saving is claimed."
        )
        return found
    assert saving is not None and percent is not None and bend_saving is not None
    bends_text = (
        f"{bend_saving} fewer bend(s)"
        if bend_saving > 0
        else f"{-bend_saving} more bend(s)"
        if bend_saving < 0
        else "the same number of bends"
    )
    if percent > SIGNIFICANT_SAVING_PERCENT:
        found.append(
            f"{name}: Syntera's verified route is {saving:.0f} mm ({percent:.1f}%) shorter with "
            f"{bends_text}, about {currency} {cost:,.0f} less."
        )
    elif drawn.assurance_pass:
        found.append(f"{name}: the drawn route is already as short as Syntera's verified route.")
    else:
        found.append(
            f"{name}: Syntera's verified route is {-saving:.0f} mm longer than drawn but passes "
            "every check the drawn route fails."
        )
    return found


def check_drawing(
    doc: Drawing, summary: DrawingSummary, folder: Path, options: AssemblyOptions
) -> PipingCheckResult:
    """Infer the 3D assembly, check each drawn pipe run, and write outputs under ``folder``."""
    output = folder / ASSEMBLY_DIR
    shutil.rmtree(output, ignore_errors=True)
    output.mkdir(parents=True)
    layout = infer_layout(doc, summary.units.mm_per_unit, options)
    report = PipingCheckReport(
        drawing_id=summary.drawing_id,
        filename=summary.filename,
        source_sha256=summary.source_sha256,
        supported=layout.supported,
        message=layout.message,
        currency=options.currency,
        workspace_mm=layout.workspace,
        views_found=layout.views,
        equipment=layout.equipment,
        assumptions=layout.assumptions,
        lines=[],
        total_drawn_length_mm=None,
        total_optimised_length_mm=None,
        total_cost_saving=None,
        findings=[],
    )
    geometries: list[LineGeometry] = []
    if layout.supported:
        assert layout.workspace is not None and layout.tube is not None
        resolution = voxel_resolution(layout.workspace)
        partners = tee_partners(layout.lines)
        for index, line in enumerate(layout.lines):
            others = [
                leg
                for other, drawn in enumerate(layout.lines)
                if other != index and other not in partners[index]
                for leg in leg_obstacles(drawn, layout.tube)
            ]
            check, geometry = _check_line(
                layout, line, index, resolution, output, options, summary, others
            )
            report.lines.append(check)
            geometries.append(geometry)
        _totals(report)
        report.audit = audit_drawn_plant(layout, geometries, report, output / PLANT_STEP)
        report.findings.append(f"Whole drawn plant: {report.audit.summary}.")
    (output / REPORT_FILE).write_text(
        json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return PipingCheckResult(report=report, lines=geometries)


def _on_path(point: Point3, points: list[Point3], tolerance: float) -> bool:
    for a, b in zip(points, points[1:], strict=False):
        ab = [b[i] - a[i] for i in range(3)]
        length = sum(v * v for v in ab)
        t = 0.0 if not length else sum((point[i] - a[i]) * ab[i] for i in range(3)) / length
        t = min(1.0, max(0.0, t))
        if math.dist(point, tuple(a[i] + t * ab[i] for i in range(3))) <= tolerance:
            return True
    return False


def tee_partners(lines: list[DrawnLine]) -> list[set[int]]:
    """For each line, the lines it is teed into or that are teed into it."""
    partners: list[set[int]] = [set() for _ in lines]
    for index, line in enumerate(lines):
        for end in line.ends:
            if end.kind != "tee":
                continue
            for other, header in enumerate(lines):
                if other != index and _on_path(end.port, header.points, 1.0):
                    partners[index].add(other)
                    partners[other].add(index)
    return partners


def leg_obstacles(line: DrawnLine, fallback: TubeSpecification) -> list[CylinderObstacle]:
    """Each straight leg of a drawn line as a cylinder another line's route must avoid."""
    radius = (line.tube or fallback).outer_diameter / 2
    legs = []
    for number, (a, b) in enumerate(zip(line.points, line.points[1:], strict=False), start=1):
        length = math.dist(a, b)
        axes = [i for i in range(3) if abs(b[i] - a[i]) > 1e-6]
        if length <= 1e-6 or len(axes) != 1:
            continue
        legs.append(
            CylinderObstacle(
                type="cylinder",
                name=f"{OTHER_LINE}{line.name} leg {number}",
                center=tuple((a[i] + b[i]) / 2 for i in range(3)),  # type: ignore[arg-type]
                radius=radius,
                height=length,
                axis="xyz"[axes[0]],  # type: ignore[arg-type]
            )
        )
    return legs


def _stub(end: LineEnd, radius: float) -> cq.Shape | None:
    if end.kind != "nozzle" or end.face is None or math.dist(end.face, end.port) <= 1e-6:
        return None
    direction = cq.Vector(*(end.port[i] - end.face[i] for i in range(3)))
    return cq.Solid.makeCylinder(
        radius, direction.Length, cq.Vector(*end.face), direction.normalized()
    )


def _end_text(end: LineEnd) -> str:
    text = {
        "nozzle": f"nozzle on {end.equipment}",
        "tee": "tee into another line",
        "battery_limit": "battery limit",
        "open": "not connected",
    }[end.kind]
    return f"{text} ({end.note})" if end.note else text


def _check_line(
    layout: InferredLayout,
    line: DrawnLine,
    index: int,
    resolution: float,
    output: Path,
    options: AssemblyOptions,
    summary: DrawingSummary,
    others: list[CylinderObstacle] | None = None,
) -> tuple[LineCheck, LineGeometry]:
    assert layout.workspace is not None and layout.tube is not None
    tube_spec = line.tube or layout.tube
    folder = output / f"line-{index + 1}"
    folder.mkdir()
    points = line.points
    if line.ends:
        start_dir, end_dir = line.ends[0].direction, line.ends[-1].direction
    else:
        start_dir = _direction(points[0], points[1])
        end_dir = _direction(points[-1], points[-2])
    title = f"{summary.filename} {line.name} (3D model inferred from drawing)"
    cardinal = line.orthogonal and not line.problems
    drawn_config = (
        _config(layout, title, points[0], start_dir, points[-1], end_dir, resolution, tube_spec)
        if cardinal
        else None
    )
    if not cardinal:
        # Angled legs carry no port direction; use the dominant axis of each end leg.
        start_dir = _cardinal(points[0], points[1])
        end_dir = _cardinal(points[-1], points[-2])
    start = _snap(points[0], resolution, layout.workspace, start_dir)
    end = _snap(points[-1], resolution, layout.workspace, end_dir)
    shift = max(math.dist(points[0], start), math.dist(points[-1], end))
    optimised_config = _config(
        layout, title, start, start_dir, end, end_dir, resolution, tube_spec, others
    )
    config_for_drawn = drawn_config or _config(
        layout, title, start, start_dir, end, end_dir, resolution, tube_spec
    )
    drawn, drawn_tube = _check_drawn(line, config_for_drawn, options)

    if options.optimise:
        result = execute_pipeline(optimised_config, folder, create_preview=False)
        built = result.tube is not None
        routed_shapes, routed_tube = result.assembly, result.tube
        optimised = _route_check(
            result.route.points if built else [],
            tube_spec.minimum_bend_radius,
            options,
            result.report if built else None,
            [] if built else [result.route.diagnostic or "no route found"],
            built,
        )
    else:
        routed_shapes, routed_tube = assembly_shapes(optimised_config), None
        optimised = _route_check(
            [], tube_spec.minimum_bend_radius, options, None, [NOT_REQUESTED], False
        )
    (folder / CONFIG_FILE).write_text(
        json.dumps(optimised_config.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8"
    )
    assembly = assembly_shapes(config_for_drawn)
    stubs = tuple(shape for end in line.ends if (shape := _stub(end, tube_spec.outer_diameter / 2)))
    if drawn_tube is not None:
        export_step([*assembly, *stubs, drawn_tube], folder / DRAWN_STEP)

    saving = percent = bend_saving = cost = None
    if optimised.assurance_pass and drawn.length_mm and optimised.length_mm is not None:
        saving = round(drawn.length_mm - optimised.length_mm, 3)
        percent = round(100 * saving / drawn.length_mm, 2)
        bend_saving = drawn.bends - optimised.bends
        cost = round((drawn.estimated_cost or 0) - (optimised.estimated_cost or 0), 2)
    findings = _findings(
        line.name,
        drawn,
        optimised,
        saving,
        percent,
        bend_saving,
        cost,
        options.currency,
        tube_spec.minimum_clearance,
    )
    if shift > 1e-6:
        findings.append(
            f"{line.name}: nozzles were moved up to {shift:.1f} mm onto Syntera's "
            f"{resolution:g} mm routing grid."
        )
    check = LineCheck(
        name=line.name,
        folder=f"{ASSEMBLY_DIR}/{folder.name}",
        line_number=line.line_number,
        outer_diameter_mm=tube_spec.outer_diameter,
        ends=[_end_text(end) for end in line.ends],
        nozzle_shift_mm=round(shift, 3),
        voxel_resolution_mm=resolution,
        drawn=drawn,
        optimised=optimised,
        length_saving_mm=saving,
        length_saving_percent=percent,
        bend_saving=bend_saving,
        cost_saving=cost,
        findings=findings,
    )
    return check, LineGeometry(optimised_config, routed_shapes, drawn_tube, routed_tube, stubs)


def audit_drawn_plant(
    layout: InferredLayout,
    geometries: list[LineGeometry],
    report: PipingCheckReport,
    step_path: Path,
) -> AssemblyAudit:
    """Audit every drawn pipe, nozzle stub, equipment item and column against each other.

    The per-line checks only see equipment; this is where two drawn lines that cross at the
    same level, or a nozzle that collides with a neighbour, are found.
    """
    assert layout.tube is not None
    parts: list[AuditPart] = []
    structures = set(layout.structures)
    for obstacle in layout.obstacles:
        role = "structure" if obstacle.name in structures else "equipment"
        parts.append(AuditPart(name=obstacle.name, role=role, shape=obstacle_shape(obstacle)))
    tees: dict[int, list[Point3]] = {}
    for index, line in enumerate(layout.lines):
        for end in line.ends:
            if end.kind != "tee":
                continue
            for other, header in enumerate(layout.lines):
                if other != index and _on_path(end.port, header.points, 1.0):
                    tees.setdefault(other, []).append(end.port)
    missing: list[str] = []
    for index, (line, geometry) in enumerate(zip(layout.lines, geometries, strict=True)):
        spec = line.tube or layout.tube
        if geometry.drawn_tube is None:
            missing.append(line.name)
            continue
        ends = [end for end in line.ends if end.kind in {"nozzle", "tee", "open"}]
        for end, shape in zip(
            [end for end in line.ends if _stub(end, 1.0) is not None], geometry.stubs, strict=True
        ):
            parts.append(
                AuditPart(
                    name=f"{line.name} nozzle on {end.equipment}",
                    role="stub",
                    shape=shape,
                    line=line.name,
                    outer_diameter=spec.outer_diameter,
                    clearance=spec.minimum_clearance,
                    junctions=(end.face, end.port),  # type: ignore[arg-type]
                    owner=end.equipment,
                )
            )
        check = report.lines[index]
        parts.append(
            AuditPart(
                name=line.name,
                role="pipe",
                shape=geometry.drawn_tube,
                line=line.name,
                outer_diameter=spec.outer_diameter,
                minimum_bend_radius=spec.minimum_bend_radius,
                clearance=spec.minimum_clearance,
                junctions=(line.points[0], line.points[-1], *tees.get(index, [])),
                ends=tuple(PipeEnd(end.port, end.direction) for end in ends),
                direct_distance=sum(abs(line.points[-1][i] - line.points[0][i]) for i in range(3)),
                length=check.drawn.length_mm,
            )
        )
    export_step([part.shape for part in parts], step_path)
    return audit_assembly(
        parts, missing_lines=missing, missing_reason="cannot be built as drawn (see its check)"
    )


def _cardinal(a: Point3, b: Point3) -> Point3:
    delta = [b[i] - a[i] for i in range(3)]
    axis = max(range(3), key=lambda i: (abs(delta[i]), -i))
    direction = [0.0, 0.0, 0.0]
    direction[axis] = math.copysign(1.0, delta[axis])
    return tuple(direction)  # type: ignore[return-value]


def _totals(report: PipingCheckReport) -> None:
    lines = report.lines
    drawn = [line.drawn.length_mm for line in lines]
    optimised = [line.optimised.length_mm for line in lines if line.optimised.assurance_pass]
    report.total_drawn_length_mm = round(sum(drawn), 3) if all(drawn) and drawn else None
    report.total_optimised_length_mm = (
        round(sum(optimised), 3) if lines and len(optimised) == len(lines) else None
    )
    savings = [line.cost_saving for line in lines if line.cost_saving is not None]
    report.total_cost_saving = round(sum(savings), 2) if savings else None
    report.findings = [finding for line in lines for finding in line.findings]
