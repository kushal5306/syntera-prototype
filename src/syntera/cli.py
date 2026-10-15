"""Command-line entry point for the deterministic Syntera demonstration."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml
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


def run_drawing(source: Path, output: Path) -> int:
    """Convert (if DWG), read and render one drawing into ``output/<drawing_id>``."""
    from syntera.drawings.intake import SUMMARY_FILE, ingest_drawing

    summary = ingest_drawing(source.read_bytes(), source.name, output)
    folder = output / summary.drawing_id
    via = summary.conversion.converter if summary.conversion else "read directly"
    print(f"Drawing: {summary.filename} ({summary.source_format.upper()}, {via})")
    print(f"Entities: {summary.entity_count}; units: {summary.units.name}")
    print(f"Dimensions: {len(summary.dimensions)}; text notes: {len(summary.texts)}")
    print(f"Summary: {folder / SUMMARY_FILE}")
    return 0


def run_drawing_check(source: Path, output: Path, options_json: str | None) -> int:
    """Build the 3D assembly from a plan drawing and check its piping as drawn."""
    from syntera.drawings.intake import DXF_FILE, ingest_drawing
    from syntera.drawings.models import AssemblyOptions
    from syntera.drawings.piping_check import ASSEMBLY_DIR, REPORT_FILE, check_drawing
    from syntera.drawings.reader import load_dxf

    options = AssemblyOptions.model_validate_json(options_json or "{}")
    summary = ingest_drawing(source.read_bytes(), source.name, output)
    folder = output / summary.drawing_id
    doc, _, _ = load_dxf(folder / DXF_FILE)
    report = check_drawing(doc, summary, folder, options).report
    print(f"Drawing: {summary.filename}")
    print(report.message)
    for finding in report.findings:
        print(f"- {finding}")
    print(f"Report: {folder / ASSEMBLY_DIR / REPORT_FILE}")
    return 0 if report.supported else 2


def run_pid_3d(source: Path, output: Path) -> int:
    """Propose a 3D plant layout for a DEXPI P&ID and route every line."""
    from syntera.pid.build import REPORT_FILE, build_plant
    from syntera.pid.dexpi import read_dexpi

    document = read_dexpi(source.read_bytes(), source.name)
    folder = output / document.source_sha256[:16] / "plant"
    report = build_plant(document, folder).report
    print(f"P&ID: {report.filename}: {len(report.equipment)} equipment item(s)")
    print(f"Lines routed and verified: {report.routed} of {len(report.routes)}")
    print(f"Total pipe: {report.total_pipe_length_mm / 1000:.1f} m, {report.total_bends} bends")
    for route in report.routes:
        if not route.routed:
            print(f"- not routed: line {route.line} ({'; '.join(route.failure_reasons)})")
    print(f"Report: {folder / REPORT_FILE}")
    return 0 if report.failed == 0 else 2


def run_compare(baseline_path: Path, output: Path) -> int:
    """Compare a stored run with a baseline skid and write ``comparison.json``."""
    from syntera.reporting.compare import COMPARISON_FILE, compare_run

    report = compare_run(output, baseline_path)
    write_json(report, output / COMPARISON_FILE)
    kind = "synthetic" if report.baseline_synthetic else "supplied"
    print(f"Baseline: {report.baseline_title} ({kind})")
    print(f"Package volume ratio: {report.package_volume_ratio:.3f} (target < 1)")
    if report.piping_length_ratio is not None:
        print(
            f"Main piping: {report.design.main_piping_length_mm:.1f} mm vs "
            f"{report.baseline.main_piping_length_mm:.1f} mm "
            f"(ratio {report.piping_length_ratio:.3f})"
        )
    if report.piping_cost_saving is not None:
        print(f"Estimated piping cost saving: {report.piping_cost_saving:.2f} {report.currency}")
    print(f"KPI 2: {'MET' if report.kpi2_met else 'NOT MET'}")
    for reason in report.validity_reasons:
        print(f"- not counted: {reason}")
    print(f"Report: {output / COMPARISON_FILE}")
    return 0 if report.design_valid else 2


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
    drawing = subparsers.add_parser(
        "drawing", help="convert a DWG/DXF drawing to DXF, an SVG view and a JSON summary"
    )
    drawing.add_argument("--input", type=Path, required=True)
    drawing.add_argument("--output", type=Path, default=Path("outputs/drawings"))
    check = subparsers.add_parser(
        "drawing-check",
        help="build a 3D assembly from a plan DWG/DXF and check its piping as drawn",
    )
    check.add_argument("--input", type=Path, required=True)
    check.add_argument("--output", type=Path, default=Path("outputs/drawings"))
    check.add_argument(
        "--options",
        help="JSON assembly options, e.g. '{\"equipment_height_mm\": 1200}'",
    )
    pid = subparsers.add_parser(
        "pid-3d", help="propose a 3D plant layout from a DEXPI XML P&ID and route every line"
    )
    pid.add_argument("--input", type=Path, required=True)
    pid.add_argument("--output", type=Path, default=Path("outputs/pid"))
    comparison = subparsers.add_parser(
        "compare", help="compare a routed run with a baseline skid (KPI 2 package size)"
    )
    comparison.add_argument("--baseline", type=Path, required=True)
    comparison.add_argument("--output", type=Path, required=True)
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
        if args.command == "drawing":
            return run_drawing(args.input, args.output)
        if args.command == "pid-3d":
            return run_pid_3d(args.input, args.output)
        if args.command == "drawing-check":
            return run_drawing_check(args.input, args.output, args.options)
        if args.command == "compare":
            return run_compare(args.baseline, args.output)
        if args.command == "web":
            import uvicorn

            from syntera.web.app import create_app

            uvicorn.run(create_app(args.config, args.output), host=args.host, port=args.port)
            return 0
    except (OSError, ValidationError, ValueError, yaml.YAMLError) as error:
        print(f"Configuration or execution error: {error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
