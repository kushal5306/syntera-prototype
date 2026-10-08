"""Command-line entry point for the deterministic Syntera demonstration."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from syntera.analysis.calculix import MANIFEST_FILE, input_sha256
from syntera.analysis.results import evaluate_acceptance
from syntera.config import load_config
from syntera.pipeline import execute_pipeline
from syntera.reporting.output import write_json
from syntera.schemas import FeaManifest, RouteResult


def run_demo(config_path: Path, output: Path) -> int:
    config = load_config(config_path)
    result = execute_pipeline(config, output)
    report = result.report

    print(f"Route found: {report.route_found}")
    print(f"Overall assurance: {'PASS' if report.overall_pass else 'FAIL'}")
    print(f"Report: {output / 'assurance_report.json'}")
    if report.failure_reasons:
        print("Reasons: " + "; ".join(report.failure_reasons))
    if result.fea is not None:
        status = "generated" if result.fea.generated else f"FAILED ({result.fea.diagnostic})"
        print(f"CalculiX decks: {status} in {output / 'fea'}")
        if not result.fea.generated:
            return 2
    return 0 if report.overall_pass else 2


def run_fea_evaluate(config_path: Path, output: Path) -> int:
    """Apply acceptance thresholds to CalculiX results solved from generated decks."""
    config = load_config(config_path)
    fea_dir = output / "fea"
    manifest = FeaManifest.model_validate_json(
        (fea_dir / MANIFEST_FILE).read_text(encoding="utf-8")
    )
    route = RouteResult.model_validate_json((output / "route.json").read_text(encoding="utf-8"))
    report = evaluate_acceptance(manifest, fea_dir, input_sha256(config, route.points))
    report_path = fea_dir / "fea_acceptance_report.json"
    write_json(report, report_path)
    print(f"FEA acceptance: {'PASS' if report.overall_pass else 'FAIL'}")
    print(f"Report: {report_path}")
    if report.failure_reasons:
        print("Reasons: " + "; ".join(report.failure_reasons))
    return 0 if report.overall_pass else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="syntera", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    demo = subparsers.add_parser("demo", help="route and verify the synthetic skid")
    demo.add_argument("--config", type=Path, required=True)
    demo.add_argument("--output", type=Path, required=True)
    evaluate = subparsers.add_parser(
        "fea-evaluate", help="apply FEA acceptance thresholds to solved CalculiX results"
    )
    evaluate.add_argument("--config", type=Path, required=True)
    evaluate.add_argument("--output", type=Path, required=True)
    web = subparsers.add_parser("web", help="launch the interactive 3D application")
    web.add_argument("--config", type=Path, default=Path("examples/demo_skid.yaml"))
    web.add_argument("--output", type=Path, default=Path("outputs/web"))
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8000)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "demo":
            return run_demo(args.config, args.output)
        if args.command == "fea-evaluate":
            return run_fea_evaluate(args.config, args.output)
        if args.command == "web":
            import uvicorn

            from syntera.web.app import create_app

            uvicorn.run(create_app(args.config, args.output), host=args.host, port=args.port)
            return 0
    except (OSError, ValidationError, ValueError) as error:
        print(f"Configuration or execution error: {error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
