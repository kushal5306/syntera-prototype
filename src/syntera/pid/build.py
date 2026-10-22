"""Turn a P&ID into a proposed 3D plant: place equipment, route every line, verify each.

Lines are routed one at a time, largest bore first. Each routed pipe becomes a solid
obstacle for the lines after it, so pipes never cross one another. Every route is
verified with the same exact checks as any Syntera route; a line that cannot be routed
is reported, never drawn through equipment.
"""

from __future__ import annotations

import json
import math
import shutil
from dataclasses import dataclass
from pathlib import Path

import cadquery as cq

from syntera.drawings.piping_check import voxel_resolution
from syntera.geometry.cad import export_step, obstacle_shape
from syntera.geometry.centreline import ArcSegment, centreline_segments
from syntera.pid.layout import Layout, NozzlePort, clearance_for, outside_diameter, place
from syntera.pid.models import PidDocument, PlacedRoute, PlantLayoutReport, Point3
from syntera.pid.rationale import line_rationale, plant_rationale
from syntera.pipeline import execute_pipeline
from syntera.rationale import RATIONALE_FILE, build_report, grid_rationale
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

REPORT_FILE = "plant_layout.json"
CONFIG_FILE = "config.json"
PLANT_STEP = "plant_assembly.step"
PLANT_MAX_VOXELS = 120_000
COST_PER_METRE_PER_DN50 = 120.0
COST_PER_BEND_PER_DN50 = 45.0


@dataclass(frozen=True)
class RouteGeometry:
    route: PlacedRoute
    tube: cq.Shape | None
    stubs: list[cq.Shape]


@dataclass(frozen=True)
class PlantResult:
    report: PlantLayoutReport
    equipment: list[cq.Shape]
    routes: list[RouteGeometry]


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


def _stub(face: Point3, port: Point3, outer: float) -> cq.Shape | None:
    length = math.dist(face, port)
    if length <= 1e-6:
        return None
    direction = cq.Vector(*(port[i] - face[i] for i in range(3))).normalized()
    return cq.Solid.makeCylinder(outer / 2, length, cq.Vector(*face), direction)


def build_plant(document: PidDocument, output: Path, *, currency: str = "USD") -> PlantResult:
    """Place, route, verify and export the whole P&ID under ``output``."""
    shutil.rmtree(output, ignore_errors=True)
    output.mkdir(parents=True)
    provisional = place(document, 250.0)
    step = voxel_resolution(provisional.workspace, PLANT_MAX_VOXELS)
    layout = provisional if step == 250.0 else place(document, step)
    grid = grid_rationale(step, provisional.workspace, PLANT_MAX_VOXELS)
    reserved = {
        terminal_id: _reserve(port, _od_of(document, terminal_id))
        for terminal_id, port in layout.ports.items()
    }
    equipment = _equipment_obstacles(layout)
    pipes: list[BoxObstacle] = []
    geometries: list[RouteGeometry] = []
    order = sorted(document.connections, key=lambda c: (-(c.nominal_diameter or 0), c.line, c.id))
    for index, connection in enumerate(order):
        outer = outside_diameter(connection.nominal_diameter)
        radius = 1.5 * outer
        start, end = layout.ports[connection.start.id], layout.ports[connection.end.id]
        folder = output / f"route-{index + 1}"
        label = f"{connection.start.label} to {connection.end.label}"
        stubs = [
            shape
            for shape in (_stub(start.face, start.port, outer), _stub(end.face, end.port, outer))
            if shape is not None
        ]
        stub_length = math.dist(start.face, start.port) + math.dist(end.face, end.port)
        reasons: list[str] = []
        tube = None
        report = None
        points: list[Point3] = []
        try:
            config = DemoConfig(
                title=f"{document.filename} line {connection.line} (proposed layout)",
                synthetic_data=True,
                workspace=Workspace(units="mm", dimensions=layout.workspace),
                obstacles=[
                    *equipment,
                    # A nozzle shared by two runs of a header is a tee: the earlier run's
                    # pipe at that nozzle is where this run joins it, not an obstacle.
                    *(
                        pipe
                        for pipe in pipes
                        if min(
                            point_to_obstacle_distance(start.port, pipe),
                            point_to_obstacle_distance(end.port, pipe),
                        )
                        >= outer / 2 + clearance_for(outer)
                    ),
                    *(
                        box
                        for terminal_id, box in sorted(reserved.items())
                        if terminal_id not in (connection.start.id, connection.end.id)
                        and box is not None
                    ),
                ],
                start_port=Port(position=start.port, direction=start.direction),
                end_port=Port(position=end.port, direction=end.direction),
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
            folder.mkdir(parents=True, exist_ok=True)
            reasons.append(str(error).splitlines()[0])
        routed = tube is not None
        length = bends = None
        if routed:
            segments = centreline_segments(points, radius)
            length = sum(segment.length for segment in segments) + stub_length
            bends = sum(isinstance(segment, ArcSegment) for segment in segments)
            pipes.extend(_pipe_boxes(f"line {connection.line}", points, outer, radius))
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
                    stub_length_mm=round(stub_length, 3),
                    estimated_cost=cost,
                    minimum_clearance_mm=(
                        round(report.minimum_measured_clearance_mm, 3)
                        if report and report.minimum_measured_clearance_mm is not None
                        else None
                    ),
                    failure_reasons=reasons,
                    folder=folder.name,
                ),
                tube=tube,
                stubs=stubs,
            )
        )

    equipment_shapes = [obstacle_shape(item) for item in equipment]
    solids = [*equipment_shapes]
    for geometry in geometries:
        if geometry.tube is not None:
            solids.append(geometry.tube)
            solids.extend(geometry.stubs)
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
        "Tees, valves and other inline items are not modelled as solids; a header with "
        "branches is routed as a chain through its terminals.",
        f"Routing grid {step:g} mm. Costs are placeholders scaled from USD 120/m and "
        "USD 45/bend at DN50.",
        "Nozzle stubs join the flange to the routed pipe; they touch their own equipment by "
        "design and are not part of the clearance check.",
    ]
    if defaulted:
        notes.append(f"Sizes are class defaults (not in the P&ID) for: {', '.join(defaulted)}.")
    return notes
