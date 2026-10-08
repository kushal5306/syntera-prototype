"""Reusable deterministic route-generation pipeline."""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path

import cadquery as cq

from syntera.analysis.calculix import write_fea_inputs
from syntera.geometry.cad import assembly_shapes, export_step, make_tube
from syntera.geometry.step_import import import_step_obstacle
from syntera.reporting.metrics import package_metrics
from syntera.reporting.output import write_json, write_preview
from syntera.routing.astar import AStarRouter
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


def execute_pipeline(
    config: DemoConfig,
    output: Path,
    *,
    create_preview: bool = True,
) -> PipelineResult:
    """Route, construct, verify, and persist one configuration."""
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
    ):
        (output / filename).unlink(missing_ok=True)
    shutil.rmtree(output / "fea", ignore_errors=True)
    step_obstacles = [item for item in config.obstacles if isinstance(item, StepObstacle)]
    if step_obstacles:
        summaries = [import_step_obstacle(obstacle).summary for obstacle in step_obstacles]
        write_json(ImportReport(obstacles=summaries), output / "import_report.json")
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
    if tube is None:
        report = failed_report(config, route, time.perf_counter() - started_at)
    else:
        export_step([*assembly, tube], output / "routed_assembly.step")
        report = verify_route(config, route, tube, started_at)
    write_json(report, output / "assurance_report.json")
    metrics = package_metrics(config, assembly, tube, route.points)
    write_json(metrics, output / "metrics.json")
    fea = None
    if config.analysis is not None and report.overall_pass:
        fea = write_fea_inputs(config, route.points, output / "fea")
    return PipelineResult(
        assembly=assembly, route=route, report=report, tube=tube, fea=fea, metrics=metrics
    )
