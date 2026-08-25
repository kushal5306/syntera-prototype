"""Command-line entry point for the deterministic Syntera demonstration."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from pydantic import ValidationError

from syntera.config import load_config
from syntera.geometry.cad import assembly_shapes, export_step, make_tube
from syntera.reporting.output import write_json, write_preview
from syntera.routing.astar import AStarRouter
from syntera.schemas import RouteResult
from syntera.spatial.occupancy import OccupancyGrid
from syntera.verification.checks import failed_report, verify_route


def run_demo(config_path: Path, output: Path) -> int:
    started_at = time.perf_counter()
    output.mkdir(parents=True, exist_ok=True)
    config = load_config(config_path)
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
    write_preview(config, route, output / "route_preview.png")
    if tube is None:
        report = failed_report(config, route, time.perf_counter() - started_at)
    else:
        export_step([*assembly, tube], output / "routed_assembly.step")
        report = verify_route(config, route, tube, started_at)
    write_json(report, output / "assurance_report.json")

    print(f"Route found: {report.route_found}")
    print(f"Overall assurance: {'PASS' if report.overall_pass else 'FAIL'}")
    print(f"Report: {output / 'assurance_report.json'}")
    if report.failure_reasons:
        print("Reasons: " + "; ".join(report.failure_reasons))
    return 0 if report.overall_pass else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="syntera", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    demo = subparsers.add_parser("demo", help="route and verify the synthetic skid")
    demo.add_argument("--config", type=Path, required=True)
    demo.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "demo":
            return run_demo(args.config, args.output)
    except (OSError, ValidationError, ValueError) as error:
        print(f"Configuration or execution error: {error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
