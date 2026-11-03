"""Reusable deterministic route-generation pipeline."""

from __future__ import annotations

import shutil
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import cadquery as cq

from syntera.analysis.calculix import write_fea_inputs
from syntera.components.fittings import (
    FITTINGS_FILE,
    FittingSet,
    place_fittings,
    verify_fittings,
)
from syntera.components.supports import SUPPORTS_FILE, SupportLayout, support_layout
from syntera.components.tables import TableError
from syntera.geometry.cad import assembly_shapes, export_step, make_tube
from syntera.geometry.step_import import import_step_obstacle
from syntera.rationale import (
    RATIONALE_FILE,
    Rationale,
    RationaleReport,
    build_report,
    config_rationale,
)
from syntera.reporting.layout_dxf import write_layout_dxf
from syntera.reporting.metrics import package_metrics
from syntera.reporting.output import write_json, write_preview
from syntera.routing.astar import AStarRouter
from syntera.rules.sizing import RuleError
from syntera.schemas import (
    AssuranceReport,
    DemoConfig,
    FeaManifest,
    ImportReport,
    PackageMetrics,
    RouteResult,
    StepObstacle,
)
from syntera.spatial.occupancy import OccupancyGrid
from syntera.verification.checks import failed_report, verify_route


@dataclass(frozen=True)
class PipelineResult:
    """Geometry and assurance produced by one deterministic execution."""

    assembly: list[cq.Shape]
    route: RouteResult
    report: AssuranceReport
    tube: cq.Shape | None
    fea: FeaManifest | None = None
    metrics: PackageMetrics | None = None
    rationale: RationaleReport | None = None
    fittings: FittingSet | None = None
    supports: SupportLayout | None = None


def execute_pipeline(
    config: DemoConfig,
    output: Path,
    *,
    create_preview: bool = True,
    rationale: Iterable[Rationale] = (),
) -> PipelineResult:
    """Route, construct, verify, and persist one configuration.

    ``rationale`` overrides the records derived from ``config`` at the same paths. Callers
    that build a configuration themselves (from a P&ID or a drawing) pass the rules and
    sources they used, so no chosen value is reported as stated by the user.
    """
    started_at = time.perf_counter()
    output.mkdir(parents=True, exist_ok=True)
    for filename in (
        "assembly.step",
        "routed_assembly.step",
        "route.json",
        "assurance_report.json",
        "route_preview.png",
        "import_report.json",
        "metrics.json",
        "layout.dxf",
        RATIONALE_FILE,
        FITTINGS_FILE,
        SUPPORTS_FILE,
    ):
        (output / filename).unlink(missing_ok=True)
    shutil.rmtree(output / "fea", ignore_errors=True)
    step_obstacles = [item for item in config.obstacles if isinstance(item, StepObstacle)]
    if step_obstacles:
        summaries = [import_step_obstacle(obstacle).summary for obstacle in step_obstacles]
        write_json(ImportReport(obstacles=summaries), output / "import_report.json")
    explained = build_report(config.title, config_rationale(config), rationale)
    write_json(explained, output / RATIONALE_FILE)
    assembly = assembly_shapes(config)
    export_step(assembly, output / "assembly.step")

    grid = OccupancyGrid.from_config(config)
    route = AStarRouter(grid, config).route()
    tube = None
    if route.found:
        try:
            tube = make_tube(
                route.points,
                config.tube.outer_diameter / 2.0,
                config.tube.minimum_bend_radius,
            )
        except (RuntimeError, ValueError) as error:
            route = RouteResult(
                found=False,
                diagnostic=f"CAD tube construction failed safely: {error}",
                expanded_nodes=route.expanded_nodes,
            )

    write_json(route, output / "route.json")
    if create_preview:
        write_preview(config, route, output / "route_preview.png")
    fittings = None
    if tube is None:
        report = failed_report(config, route, time.perf_counter() - started_at)
    else:
        report = verify_route(config, route, tube, started_at)
        parts: list[cq.Shape] = [tube]
        if config.fittings is not None:
            fittings, report, parts = _with_fittings(config, route, report)
        export_step([*assembly, *parts], output / "routed_assembly.step")
    supports = None
    if config.supports is not None and tube is not None:
        try:
            supports = support_layout(config, route.points)
        except (RuleError, ValueError) as error:
            reasons = [*report.failure_reasons, f"supports could not be placed: {error}"]
            report = report.model_copy(update={"overall_pass": False, "failure_reasons": reasons})
        else:
            write_json(supports, output / SUPPORTS_FILE)
    write_json(report, output / "assurance_report.json")
    if fittings is not None:
        write_json(fittings.report, output / FITTINGS_FILE)
    write_layout_dxf(config, route, report, output / "layout.dxf")
    metrics = package_metrics(config, assembly, tube, route.points)
    write_json(metrics, output / "metrics.json")
    fea = None
    if config.analysis is not None and report.overall_pass:
        held = [point.position for point in supports.supports] if supports else []
        fea = write_fea_inputs(config, route.points, output / "fea", held)
    return PipelineResult(
        supports=supports,
        fittings=fittings,
        assembly=assembly,
        route=route,
        report=report,
        tube=tube,
        fea=fea,
        metrics=metrics,
        rationale=explained,
    )


def _with_fittings(
    config: DemoConfig, route: RouteResult, report: AssuranceReport
) -> tuple[FittingSet | None, AssuranceReport, list[cq.Shape]]:
    """Place and check the line's fittings; a failing fitting fails the assurance.

    The routed STEP then holds the piping as built: straight pipe, elbows and flanges.
    The single swept tube remains what the route checks measured.
    """
    assert config.fittings is not None
    try:
        placed = place_fittings(
            route.points,
            config.tube.outer_diameter,
            config.tube.minimum_bend_radius,
            config.fittings,
            start_direction=config.start_port.direction,
            end_direction=config.end_port.direction,
        )
    except (TableError, ValueError) as error:
        reasons = [*report.failure_reasons, f"fittings could not be placed: {error}"]
        failed = report.model_copy(update={"overall_pass": False, "failure_reasons": reasons})
        return None, failed, []
    checked = verify_fittings(config, placed)
    placed.report = checked
    if not checked.overall_pass:
        reasons = [*report.failure_reasons, *checked.failure_reasons]
        report = report.model_copy(update={"overall_pass": False, "failure_reasons": reasons})
    pipes = [
        make_tube([piece.start, piece.end], config.tube.outer_diameter / 2.0, 1.0)
        for piece in checked.straight_pieces
    ]
    elbows = [placed.shapes[f.id] for f in checked.fittings if f.kind != "flange"]
    return placed, report, [*pipes, *elbows, *placed.flanges]
