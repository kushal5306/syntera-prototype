"""Turn a P&ID into a proposed 3D plant: place equipment, route every line, audit it.

Lines are routed one at a time, largest bore first. Each routed pipe becomes a solid
obstacle for the lines after it, so pipes never cross one another. Every route is
verified with the same exact checks as any Syntera route, and the finished plant is then
audited as one assembly. A line that cannot be routed is reported, never drawn through
equipment.
"""

from __future__ import annotations

import json
import math
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import cadquery as cq

from syntera.drawings.piping_check import voxel_resolution
from syntera.geometry.cad import export_step, obstacle_shape
from syntera.geometry.centreline import ArcSegment, centreline_segments
from syntera.pid.layout import Layout, NozzlePort, clearance_for, outside_diameter, place
from syntera.pid.models import (
    PidConnection,
    PidDocument,
    PlacedRoute,
    PlantLayoutReport,
    Point3,
    RoutingTrial,
)
from syntera.pid.rationale import line_rationale, plant_rationale
from syntera.pipeline import execute_pipeline
from syntera.rationale import RATIONALE_FILE, Rationale, build_report, grid_rationale
from syntera.reporting.output import write_json
from syntera.schemas import (
    BoxObstacle,
    CylinderObstacle,
    DemoConfig,
    Port,
    RoutingWeights,
    TubeSpecification,
    Workspace,
)
from syntera.spatial.occupancy import point_to_obstacle_distance
from syntera.verification.assembly_audit import (
    DETOUR_ADVISORY_RATIO,
    AssemblyAudit,
    AuditPart,
    PipeEnd,
    audit_assembly,
)

REPORT_FILE = "plant_layout.json"
CONFIG_FILE = "config.json"
PLANT_STEP = "plant_assembly.step"
PLANT_MAX_VOXELS = 120_000
COST_PER_METRE_PER_DN50 = 120.0
COST_PER_BEND_PER_DN50 = 45.0


@dataclass(frozen=True)
class Stub:
    """A straight pipe piece outside the routed tube: a nozzle stub or a tee branch."""

    name: str
    kind: Literal["nozzle", "battery_limit", "tee"]
    shape: cq.Shape
    start: Point3  # flange point on the equipment, skid edge, or tee point on the header
    end: Point3  # where the routed tube joins it
    owner: str | None  # equipment tag a nozzle stub is welded to


@dataclass(frozen=True)
class RouteGeometry:
    route: PlacedRoute
    tube: cq.Shape | None
    stubs: list[Stub]
    ends: tuple[Port, Port] | None = None
    direct_distance: float | None = None


@dataclass(frozen=True)
class PlantResult:
    report: PlantLayoutReport
    equipment: list[cq.Shape]
    routes: list[RouteGeometry]


@dataclass(frozen=True)
class _Pass:
    strategy: str
    routes: list[RouteGeometry]
    audit: AssemblyAudit
    folder: Path

    @property
    def failed(self) -> int:
        return sum(not g.route.routed for g in self.routes)

    @property
    def length(self) -> float:
        return sum(g.route.length_mm or 0.0 for g in self.routes)

    @property
    def score(self) -> tuple[int, int, float, int]:
        """Lower is better: unrouted lines, then failing audit findings, then length, bends."""
        findings = sum(len(r.findings) for r in self.audit.rules if r.status == "fail")
        bends = sum(g.route.bends for g in self.routes)
        return (self.failed, findings, round(self.length, 3), bends)


def _equipment_obstacles(layout: Layout) -> list[BoxObstacle | CylinderObstacle]:
    obstacles: list[BoxObstacle | CylinderObstacle] = []
    for item in layout.equipment:
        if item.shape == "cylinder":
            length_axis = 0 if item.axis == "x" else 2
            radius = item.size[1] / 2 if item.axis == "x" else item.size[0] / 2
            obstacles.append(
                CylinderObstacle(
                    type="cylinder",
                    name=item.tag,
                    center=item.center,
                    radius=radius,
                    height=item.size[length_axis],
                    axis=item.axis,
                )
            )
        else:
            obstacles.append(
                BoxObstacle(type="box", name=item.tag, center=item.center, size=item.size)
            )
    return obstacles


def _pipe_boxes(name: str, points: list[Point3], outer: float, radius: float) -> list[BoxObstacle]:
    """Conservative boxes around a routed pipe: each leg, plus a square at each bend."""
    half = outer / 2
    boxes: list[BoxObstacle] = []
    for index, (a, b) in enumerate(zip(points, points[1:], strict=False)):
        low = [min(a[i], b[i]) - half for i in range(3)]
        high = [max(a[i], b[i]) + half for i in range(3)]
        boxes.append(_box(f"{name} leg {index + 1}", low, high))
    for index, corner in enumerate(points[1:-1], start=1):
        low = [corner[i] - radius - half for i in range(3)]
        high = [corner[i] + radius + half for i in range(3)]
        boxes.append(_box(f"{name} bend {index}", low, high))
    return boxes


def _box(name: str, low: list[float], high: list[float]) -> BoxObstacle:
    return BoxObstacle(
        type="box",
        name=name,
        center=tuple((low[i] + high[i]) / 2 for i in range(3)),  # type: ignore[arg-type]
        size=tuple(max(high[i] - low[i], 1e-3) for i in range(3)),  # type: ignore[arg-type]
    )


def _od_of(document: PidDocument, terminal_id: str) -> float:
    sizes = [
        outside_diameter(c.nominal_diameter)
        for c in document.connections
        if terminal_id in (c.start.id, c.end.id)
    ]
    return max(sizes, default=outside_diameter(None))


def _reserve(port: NozzlePort, outer: float) -> BoxObstacle | None:
    """Keep other lines out of a nozzle's stub so its own line can still start there."""
    if math.dist(port.face, port.port) <= 1e-6:
        return None
    half = outer / 2
    low = [min(port.face[i], port.port[i]) - half for i in range(3)]
    high = [max(port.face[i], port.port[i]) + half for i in range(3)]
    return _box(f"nozzle {port.terminal_id} access", low, high)


def _cylinder(face: Point3, port: Point3, outer: float) -> cq.Shape | None:
    length = math.dist(face, port)
    if length <= 1e-6:
        return None
    direction = cq.Vector(*(port[i] - face[i] for i in range(3))).normalized()
    return cq.Solid.makeCylinder(outer / 2, length, cq.Vector(*face), direction)


def _free(point: Point3, obstacles: list, workspace: Point3, inflation: float) -> bool:
    inside = all(inflation <= point[i] <= workspace[i] - inflation for i in range(3))
    return inside and all(point_to_obstacle_distance(point, o) >= inflation for o in obstacles)


def _tee(
    port: NozzlePort,
    header_outer: float,
    outer: float,
    toward: Point3,
    obstacles: list,
    workspace: Point3,
    step: float,
) -> tuple[Point3, Point3] | None:
    """Branch point clear of the header for a second line sharing a nozzle.

    The header already leaves the nozzle along its axis. The branch leaves the header at the
    port, square to it, far enough out that the two pipes keep their clearance; directions
    towards the line's other end are tried first.
    """
    gap = header_outer / 2 + outer / 2 + clearance_for(max(header_outer, outer))
    reach = (math.floor(gap / step) + 1) * step
    axis = port.direction
    choices = [
        d
        for d in ((1.0, 0, 0), (-1.0, 0, 0), (0, 1.0, 0), (0, -1.0, 0), (0, 0, 1.0), (0, 0, -1.0))
        if abs(sum(d[i] * axis[i] for i in range(3))) < 0.5
    ]
    heading = [toward[i] - port.port[i] for i in range(3)]
    choices.sort(key=lambda d: (-sum(d[i] * heading[i] for i in range(3)), d))
    inflation = outer / 2 + clearance_for(outer)
    for extra in range(4):
        for d in choices:
            branch = tuple(port.port[i] + d[i] * (reach + extra * step) for i in range(3))
            if _free(branch, obstacles, workspace, inflation):  # type: ignore[arg-type]
                return branch, d  # type: ignore[return-value]
    return None


def _manhattan(a: Point3, b: Point3) -> float:
    return sum(abs(a[i] - b[i]) for i in range(3))


def _route_pass(
    document: PidDocument,
    layout: Layout,
    step: float,
    order: list[PidConnection],
    output: Path,
    grid: Rationale,
) -> list[RouteGeometry]:
    """Route every connection in ``order``; each routed pipe is an obstacle for the next.

    The first line routed to a nozzle owns it and its stub. A later line at the same nozzle
    joins through a tee: a short branch square to the first pipe, so the two never overlap.
    """
    reserved = {
        terminal_id: _reserve(port, _od_of(document, terminal_id))
        for terminal_id, port in layout.ports.items()
    }
    equipment = _equipment_obstacles(layout)
    tag_of = {e.id: e.tag for e in document.equipment}
    folder_of = {c.id: f"route-{i + 1}" for i, c in enumerate(_default_order(document))}
    pipes: list[BoxObstacle] = []
    claimed: dict[str, float] = {}  # terminal id -> OD of the line that owns its nozzle
    geometries: list[RouteGeometry] = []
    for connection in order:
        outer = outside_diameter(connection.nominal_diameter)
        radius = 1.5 * outer
        folder = output / folder_of[connection.id]
        label = f"{connection.start.label} to {connection.end.label}"
        obstacles = [
            *equipment,
            *pipes,
            *(
                box
                for terminal_id, box in sorted(reserved.items())
                if terminal_id not in (connection.start.id, connection.end.id) and box is not None
            ),
        ]
        reasons: list[str] = []
        stubs: list[Stub] = []
        tees: list[str] = []
        ports: list[Port] = []
        a, b = connection.start, connection.end
        for terminal, other in ((a, b), (b, a)):
            nozzle = layout.ports[terminal.id]
            if terminal.id not in claimed:
                shape = _cylinder(nozzle.face, nozzle.port, outer)
                if shape is not None:
                    owner = tag_of.get(terminal.equipment_id or "")
                    if owner:
                        stub = Stub(f"nozzle {terminal.label}", "nozzle", shape, nozzle.face,
                                    nozzle.port, owner)  # fmt: skip
                    else:
                        stub = Stub(f"{terminal.label} stub", "battery_limit", shape,
                                    nozzle.face, nozzle.port, None)  # fmt: skip
                    stubs.append(stub)
                ports.append(Port(position=nozzle.port, direction=nozzle.direction))
                continue
            tee = _tee(
                nozzle, claimed[terminal.id], outer, layout.ports[other.id].port,
                obstacles, layout.workspace, step,
            )  # fmt: skip
            if tee is None:
                reasons.append(f"no clear direction for a tee branch at {terminal.label}")
                break
            branch, direction = tee
            shape = _cylinder(nozzle.port, branch, outer)
            assert shape is not None
            stubs.append(
                Stub(
                    f"tee at {terminal.label} for {label}", "tee", shape, nozzle.port, branch, None
                )  # fmt: skip
            )
            tees.append(terminal.label)
            ports.append(Port(position=branch, direction=direction))
        tube = None
        report = None
        points: list[Point3] = []
        if not reasons:
            try:
                config = DemoConfig(
                    title=f"{document.filename} line {connection.line} (proposed layout)",
                    synthetic_data=True,
                    workspace=Workspace(units="mm", dimensions=layout.workspace),
                    obstacles=obstacles,
                    start_port=ports[0],
                    end_port=ports[1],
                    tube=TubeSpecification(
                        outer_diameter=outer,
                        minimum_bend_radius=radius,
                        minimum_clearance=clearance_for(outer),
                    ),
                    voxel_resolution=step,
                    routing_cost_weights=RoutingWeights(
                        bend=4 * step, obstacle_proximity=step, port_direction=1000
                    ),
                )
                result = execute_pipeline(
                    config,
                    folder,
                    create_preview=False,
                    rationale=[*line_rationale(connection.nominal_diameter, outer, step), grid],
                )
                (folder / CONFIG_FILE).write_text(
                    json.dumps(config.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8"
                )
                tube, report = result.tube, result.report
                if tube is None:
                    reasons.append(result.route.diagnostic or "no route found")
                else:
                    points = result.route.points
                    reasons.extend(report.failure_reasons)
            except ValueError as error:
                reasons.append(str(error).splitlines()[0])
        folder.mkdir(parents=True, exist_ok=True)
        routed = tube is not None
        length = bends = None
        if routed:
            segments = centreline_segments(points, radius)
            stub_length = sum(math.dist(stub.start, stub.end) for stub in stubs)
            length = sum(segment.length for segment in segments) + stub_length
            bends = sum(isinstance(segment, ArcSegment) for segment in segments)
            pipes.extend(_pipe_boxes(f"line {connection.line}", points, outer, radius))
            for terminal in (connection.start, connection.end):
                claimed.setdefault(terminal.id, outer)
            for stub in stubs:
                if stub.kind == "tee":
                    pipes.append(_cylinder_box(stub, outer))
        else:
            stubs = []
        scale = outer / outside_diameter(50)
        cost = (
            round(
                (length or 0) / 1000 * COST_PER_METRE_PER_DN50 * scale
                + (bends or 0) * COST_PER_BEND_PER_DN50 * scale,
                2,
            )
            if routed
            else None
        )
        faces = (layout.ports[connection.start.id].face, layout.ports[connection.end.id].face)
        geometries.append(
            RouteGeometry(
                route=PlacedRoute(
                    connection_id=connection.id,
                    line=connection.line,
                    label=label,
                    outer_diameter_mm=outer,
                    routed=routed,
                    assurance_pass=bool(report and routed and report.overall_pass),
                    length_mm=round(length, 3) if length is not None else None,
                    bends=bends or 0,
                    stub_length_mm=round(sum(math.dist(s.start, s.end) for s in stubs), 3),
                    estimated_cost=cost,
                    minimum_clearance_mm=(
                        round(report.minimum_measured_clearance_mm, 3)
                        if report and report.minimum_measured_clearance_mm is not None
                        else None
                    ),
                    failure_reasons=reasons,
                    folder=folder.name,
                    tees=tees,
                ),
                tube=tube,
                stubs=stubs,
                ends=(ports[0], ports[1]) if routed else None,
                direct_distance=_manhattan(*faces),
            )
        )
    return sorted(geometries, key=lambda g: int(g.route.folder.split("-")[1]))


def _cylinder_box(stub: Stub, outer: float) -> BoxObstacle:
    half = outer / 2
    low = [min(stub.start[i], stub.end[i]) - half for i in range(3)]
    high = [max(stub.start[i], stub.end[i]) + half for i in range(3)]
    return _box(stub.name, low, high)


def _default_order(document: PidDocument) -> list[PidConnection]:
    """Largest bore first, then by line and id."""
    return sorted(document.connections, key=lambda c: (-(c.nominal_diameter or 0), c.line, c.id))


def audit_plant(
    layout: Layout, equipment: list[cq.Shape], routes: list[RouteGeometry]
) -> AssemblyAudit:
    """Audit the proposed plant as one assembly (see ``syntera.verification.assembly_audit``)."""
    parts = [
        AuditPart(name=item.tag, role="equipment", shape=shape)
        for item, shape in zip(layout.equipment, equipment, strict=True)
    ]
    for geometry in routes:
        route = geometry.route
        if geometry.tube is None or geometry.ends is None:
            continue
        outer = route.outer_diameter_mm
        start, end = geometry.ends
        parts.append(
            AuditPart(
                name=f"line {route.line} ({route.label})",
                role="pipe",
                shape=geometry.tube,
                line=route.line,
                outer_diameter=outer,
                minimum_bend_radius=1.5 * outer,
                clearance=clearance_for(outer),
                junctions=(start.position, end.position),
                ends=(
                    PipeEnd(start.position, start.direction),
                    PipeEnd(end.position, end.direction),
                ),
                direct_distance=geometry.direct_distance,
                length=route.length_mm,
            )
        )
        for stub in geometry.stubs:
            parts.append(
                AuditPart(
                    name=stub.name,
                    role="stub",
                    shape=stub.shape,
                    line=route.line,
                    outer_diameter=outer,
                    clearance=clearance_for(outer),
                    junctions=(stub.start, stub.end),
                    owner=stub.owner,
                )
            )
    missing = [f"line {g.route.line} ({g.route.label})" for g in routes if not g.route.routed]
    return audit_assembly(parts, missing_lines=missing)


def _problem_first(document: PidDocument, first: _Pass) -> list[PidConnection]:
    """Unrouted lines first, then the worst detours, then the rest in the default order."""
    ratio = {
        g.route.connection_id: (g.route.length_mm or 0) / g.direct_distance
        for g in first.routes
        if g.route.routed and g.direct_distance
    }
    failed = {g.route.connection_id for g in first.routes if not g.route.routed}
    order = _default_order(document)
    position = {c.id: i for i, c in enumerate(order)}
    return sorted(
        order,
        key=lambda c: (
            c.id not in failed,
            -ratio.get(c.id, 0.0) if ratio.get(c.id, 0.0) > DETOUR_ADVISORY_RATIO else 0.0,
            position[c.id],
        ),
    )


def _short_first(document: PidDocument, layout: Layout) -> list[PidConnection]:
    """Shortest connections first, so they take the direct paths before long runs box them in."""
    order = _default_order(document)
    position = {c.id: i for i, c in enumerate(order)}
    return sorted(
        order,
        key=lambda c: (
            _manhattan(layout.ports[c.start.id].face, layout.ports[c.end.id].face),
            position[c.id],
        ),
    )


def build_plant(
    document: PidDocument, output: Path, *, currency: str = "USD", optimise: bool = True
) -> PlantResult:
    """Place, route, audit and export the whole P&ID under ``output``.

    With ``optimise``, a plant whose first pass leaves lines unrouted, fails the audit or has
    long detours is routed again in other deterministic orders (rip-up and reroute), and the
    best pass is kept: fewest unrouted lines, then fewest audit findings, then shortest pipe.
    Hard constraints are never traded for length.
    """
    shutil.rmtree(output, ignore_errors=True)
    output.mkdir(parents=True)
    provisional = place(document, 250.0)
    step = voxel_resolution(provisional.workspace, PLANT_MAX_VOXELS)
    layout = provisional if step == 250.0 else place(document, step)
    grid = grid_rationale(step, provisional.workspace, PLANT_MAX_VOXELS)
    equipment_shapes = [obstacle_shape(item) for item in _equipment_obstacles(layout)]

    def run(strategy: str, order: list[PidConnection]) -> _Pass:
        folder = output / f".pass-{strategy}"
        routes = _route_pass(document, layout, step, order, folder, grid)
        return _Pass(strategy, routes, audit_plant(layout, equipment_shapes, routes), folder)

    passes = [run("largest-bore-first", _default_order(document))]
    first = passes[0]
    broken = first.failed or not first.audit.overall_pass
    detours = any(r.status == "advisory" for r in first.audit.rules)
    if optimise and (broken or detours) and len(document.connections) > 1:
        # Detours alone are worth one reroute of the worst lines first; unrouted lines or
        # audit failures are worth every strategy.
        strategies = [("problem-lines-first", _problem_first(document, first))]
        if broken:
            strategies.append(("shortest-first", _short_first(document, layout)))
        tried = {tuple(c.id for c in _default_order(document))}
        for strategy, order in strategies:
            key = tuple(c.id for c in order)
            if key not in tried:
                tried.add(key)
                passes.append(run(strategy, order))
    best = min(passes, key=lambda p: p.score)
    for item in sorted(best.folder.iterdir()):
        item.rename(output / item.name)
    for trial in passes:
        shutil.rmtree(trial.folder, ignore_errors=True)
    geometries = best.routes

    solids = [*equipment_shapes]
    for geometry in geometries:
        if geometry.tube is not None:
            solids.append(geometry.tube)
            solids.extend(stub.shape for stub in geometry.stubs)
    export_step(solids, output / PLANT_STEP)
    routes = [g.route for g in geometries]
    done = [r for r in routes if r.routed]
    report = PlantLayoutReport(
        source_sha256=document.source_sha256,
        filename=document.filename,
        workspace_mm=layout.workspace,
        footprint_m2=round(layout.workspace[0] * layout.workspace[1] / 1e6, 3),
        voxel_resolution_mm=step,
        currency=currency,
        equipment=layout.equipment,
        routes=routes,
        routed=len(done),
        failed=len(routes) - len(done),
        total_pipe_length_mm=round(sum(r.length_mm or 0 for r in done), 3),
        total_bends=sum(r.bends for r in done),
        total_cost=round(sum(r.estimated_cost or 0 for r in done), 2),
        layout_score_mm=round(layout.score_mm, 3),
        assumptions=_assumptions(layout, step),
        skipped=document.skipped,
        audit=best.audit,
        optimisation=[
            RoutingTrial(
                strategy=trial.strategy,
                routed=len(trial.routes) - trial.failed,
                failed=trial.failed,
                audit_pass=trial.audit.overall_pass,
                audit_findings=trial.score[1],
                total_pipe_length_mm=round(trial.length, 3),
                total_bends=trial.score[3],
                chosen=trial is best,
            )
            for trial in passes
        ],
    )
    write_json(
        build_report(
            f"{document.filename} proposed layout", plant_rationale(document, layout, step), [grid]
        ),
        output / RATIONALE_FILE,
    )
    (output / REPORT_FILE).write_text(
        json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return PlantResult(report=report, equipment=equipment_shapes, routes=geometries)


def _assumptions(layout: Layout, step: float) -> list[str]:
    defaulted = [e.tag for e in layout.equipment if e.size_source == "default"]
    notes = [
        "A P&ID gives connectivity, not positions: this is a proposed layout that satisfies "
        "it, not the as-built plant.",
        "Equipment is placed on a grid with 1500 mm access gaps and arranged to shorten "
        "connected nozzles' straight-line distance.",
        "Nozzles face the item they connect to, at up to 800 mm above the item's base.",
        "Pipe OD follows the line's nominal size (ISO 4200); bend radius 1.5 x OD; clearance "
        "half the OD, at least 25 mm.",
        "Lines are routed largest bore first; each routed pipe is an obstacle for the next.",
        "Valves and other inline items are not modelled as solids; a header with branches "
        "is routed as a chain through its terminals, and a second run at a shared nozzle "
        "joins the first through a tee branch square to it.",
        f"Routing grid {step:g} mm. Costs are placeholders scaled from USD 120/m and "
        "USD 45/bend at DN50.",
        "Nozzle stubs join the flange to the routed pipe; they touch their own equipment by "
        "design. The design audit checks every solid against every other one.",
    ]
    if defaulted:
        notes.append(f"Sizes are class defaults (not in the P&ID) for: {', '.join(defaulted)}.")
    return notes
