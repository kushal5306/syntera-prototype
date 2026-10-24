"""Command-line entry point for the deterministic Syntera demonstration."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

import yaml
from pydantic import ValidationError

from syntera.analysis.calculix import MANIFEST_FILE, input_sha256
from syntera.analysis.results import evaluate_acceptance
from syntera.config import load_config, load_sized_config
from syntera.pipeline import execute_pipeline
from syntera.reporting.output import write_json
from syntera.schemas import FeaManifest, RouteResult

ComputeStep = Callable[[], int]


def run_demo(config_path: Path, output: Path) -> int:
    config, sizing = load_sized_config(config_path)
    result = execute_pipeline(config, output, rationale=sizing)
    report = result.report

    print(f"Route found: {report.route_found}")
    print(f"Overall assurance: {'PASS' if report.overall_pass else 'FAIL'}")
    print(f"Report: {output / 'assurance_report.json'}")
    if sizing and result.rationale is not None:
        od = result.rationale.get("tube.outer_diameter")
        print(f"Line sized by rules: OD {od.value:g} mm ({od.rule_id})")
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
    for trial in report.optimisation:
        kept = " (kept)" if trial.chosen else ""
        print(
            f"Routing order {trial.strategy}: {trial.routed} routed, "
            f"{trial.audit_findings} audit failure(s), "
            f"{trial.total_pipe_length_mm / 1000:.1f} m{kept}"
        )
    audit = report.audit
    if audit is not None:
        print(f"Design audit: {'PASS' if audit.overall_pass else 'FAIL'}: {audit.summary}")
        for rule in audit.rules:
            for finding in rule.findings:
                label = "fail" if rule.status == "fail" else "review"
                print(f"- {label} [{rule.id}]: {finding.message}")
    print(f"Report: {folder / REPORT_FILE}")
    return 0 if report.failed == 0 and (audit is None or audit.overall_pass) else 2


def run_audit_step(source: Path, output: Path, max_pipe_od: float) -> int:
    """Audit every solid of a STEP assembly against every other one."""
    from syntera.verification.step_audit import audit_step

    result = audit_step(source, max_pipe_od=max_pipe_od)
    audit = result.audit
    sizes = ", ".join(f"{size:g}" for size in result.sizes_mm) or "none"
    print(f"STEP: {source.name}: {result.pipes} pipe solid(s) (OD {sizes} mm), "
          f"{result.equipment} other solid(s)")  # fmt: skip
    print(f"Design audit: {'PASS' if audit.overall_pass else 'FAIL'}: {audit.summary}")
    for rule in audit.rules:
        for finding in rule.findings:
            label = "fail" if rule.status == "fail" else "review"
            print(f"- {label} [{rule.id}]: {finding.message}")
    output.mkdir(parents=True, exist_ok=True)
    write_json(audit, output / "assembly_audit.json")
    print(f"Report: {output / 'assembly_audit.json'}")
    return 0 if audit.overall_pass else 2


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


def run_timed(log: Path | None, task: str | None, phase: str, step: ComputeStep) -> int:
    """Run a compute step, recording its wall-clock time in the KPI 1 log when asked."""
    if log is None:
        return step()
    from syntera.reporting.timelog import append_event, make_event

    if task is None:
        raise ValueError("--task is required with --time-log")
    append_event(log, make_event(task, phase, "start", actor="syntera"))
    try:
        return step()
    finally:
        append_event(log, make_event(task, phase, "stop", actor="syntera"))


def run_time(action: str, log: Path, task: str, phase: str, note: str | None) -> int:
    """Record the start or stop of an engineer's phase in the KPI 1 time log."""
    from syntera.reporting.timelog import append_event, make_event

    event = make_event(task, phase, action, note=note)
    append_event(log, event)
    print(f"{event.at.isoformat()} {action} {task}/{phase} -> {log}")
    return 0


def _duration(hours: float) -> str:
    return f"{hours * 60:.1f} min" if hours < 1 else f"{hours:.2f} h"


def run_kpi1(log: Path, baseline_path: Path, output: Path) -> int:
    """Compute KPI 1 from a time log and a manual baseline; write ``kpi1_report.json``."""
    from syntera.reporting.timelog import KPI1_FILE, kpi1_from_files

    report = kpi1_from_files(log, baseline_path)
    write_json(report, output / KPI1_FILE)
    kind = "synthetic" if report.baseline_synthetic else "supplied"
    print(f"Manual baseline: {report.baseline_title} ({kind})")
    for task in report.tasks:
        if task.measured:
            estimate = ", manual hours estimated" if task.manual_estimated else ""
            print(
                f"- {task.task}: {_duration(task.ai_hours)} with Syntera vs "
                f"{_duration(task.manual_hours)} manual (ratio {task.ratio:.4f}{estimate})"
            )
        else:
            print(f"- {task.task}: not measured yet")
    if report.ratio is not None:
        print(f"KPI 1 ratio over measured tasks: {report.ratio:.4f} (target < 1)")
    print(f"KPI 1: {'MET' if report.kpi1_met else 'NOT MET'}")
    for error in report.log_errors:
        print(f"- log error: {error}")
    print(f"Report: {output / KPI1_FILE}")
    return 0 if report.log_valid else 2


def _add_time_log(command: argparse.ArgumentParser) -> None:
    command.add_argument("--time-log", type=Path, help="append compute time to this KPI 1 log")
    command.add_argument("--task", help="benchmark task name used in the time log")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="syntera", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    demo = subparsers.add_parser("demo", help="route and verify the synthetic skid")
    demo.add_argument("--config", type=Path, required=True)
    demo.add_argument("--output", type=Path, required=True)
    _add_time_log(demo)
    evaluate = subparsers.add_parser(
        "fea-evaluate", help="apply FEA acceptance thresholds to solved CalculiX results"
    )
    evaluate.add_argument("--config", type=Path, required=True)
    evaluate.add_argument("--output", type=Path, required=True)
    _add_time_log(evaluate)
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
    audit = subparsers.add_parser(
        "audit-step",
        help="audit a STEP assembly for clashes, clearances, bend radii and unjoined ends",
    )
    audit.add_argument("--input", type=Path, required=True)
    audit.add_argument("--output", type=Path, default=Path("outputs/audit"))
    audit.add_argument(
        "--max-pipe-od", type=float, default=610.0,
        help="largest outside diameter, mm, treated as pipe; bigger round solids are vessels",
    )  # fmt: skip
    comparison = subparsers.add_parser(
        "compare", help="compare a routed run with a baseline skid (KPI 2 package size)"
    )
    comparison.add_argument("--baseline", type=Path, required=True)
    comparison.add_argument("--output", type=Path, required=True)
    timing = subparsers.add_parser("time", help="record a phase start or stop for KPI 1")
    timing.add_argument("action", choices=["start", "stop"])
    timing.add_argument("--log", type=Path, required=True)
    timing.add_argument("--task", required=True)
    timing.add_argument("--phase", required=True)
    timing.add_argument("--note")
    kpi1 = subparsers.add_parser(
        "kpi1", help="KPI 1: Syntera workflow hours over manual hours, from a time log"
    )
    kpi1.add_argument("--log", type=Path, required=True)
    kpi1.add_argument("--baseline", type=Path, required=True)
    kpi1.add_argument("--output", type=Path, required=True)
    web = subparsers.add_parser("web", help="launch the interactive 3D application")
    web.add_argument("--config", type=Path, default=Path("examples/demo_skid.yaml"))
    web.add_argument("--output", type=Path, default=Path("outputs/web"))
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8000)
    web.add_argument(
        "--manual-baseline",
        type=Path,
        default=Path("examples/manual_baseline.yaml"),
        help="manual-hours baseline used for the live KPI 1 ratio",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "demo":
            return run_timed(
                args.time_log, args.task, "generate", lambda: run_demo(args.config, args.output)
            )
        if args.command == "fea-evaluate":
            return run_timed(
                args.time_log,
                args.task,
                "fea-check",
                lambda: run_fea_evaluate(args.config, args.output),
            )
        if args.command == "time":
            return run_time(args.action, args.log, args.task, args.phase, args.note)
        if args.command == "kpi1":
            return run_kpi1(args.log, args.baseline, args.output)
        if args.command == "drawing":
            return run_drawing(args.input, args.output)
        if args.command == "audit-step":
            return run_audit_step(args.input, args.output, args.max_pipe_od)
        if args.command == "pid-3d":
            return run_pid_3d(args.input, args.output)
        if args.command == "drawing-check":
            return run_drawing_check(args.input, args.output, args.options)
        if args.command == "compare":
            return run_compare(args.baseline, args.output)
        if args.command == "web":
            import uvicorn

            from syntera.web.app import create_app

            app = create_app(args.config, args.output, args.manual_baseline)
            uvicorn.run(app, host=args.host, port=args.port)
            return 0
    except (OSError, ValidationError, ValueError, yaml.YAMLError) as error:
        print(f"Configuration or execution error: {error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
