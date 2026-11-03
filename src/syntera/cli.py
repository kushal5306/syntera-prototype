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
from syntera.pipeline import execute_pipeline
from syntera.reporting.output import write_json
from syntera.schemas import FeaManifest, RouteResult

ComputeStep = Callable[[], int]


def run_demo(config_path: Path, output: Path, line_id: str | None = None) -> int:
    """Route and verify one line of a design document (or a YAML config converted to one).

    The run configuration and rationale are derived from the design document, and the
    document is written to ``output/design.json`` with the route the pipeline produced.
    """
    from syntera.design import DESIGN_FILE, load_run, save_design, with_route

    design, config, records = load_run(config_path, line_id)
    line = design.line(line_id)
    (output / DESIGN_FILE).unlink(missing_ok=True)
    result = execute_pipeline(config, output, rationale=records)
    report = result.report
    save_design(with_route(design, line.id, result.route), output / DESIGN_FILE)

    print(f"Route found: {report.route_found}")
    print(f"Overall assurance: {'PASS' if report.overall_pass else 'FAIL'}")
    print(f"Report: {output / 'assurance_report.json'}")
    print(f"Design: {output / DESIGN_FILE} (line {line.id})")
    if result.rationale is not None:
        od = result.rationale.get("tube.outer_diameter")
        if od.source == "rule":
            print(f"Line sized by rules: OD {od.value:g} mm ({od.rule_id})")
    if report.failure_reasons:
        print("Reasons: " + "; ".join(report.failure_reasons))
    if result.fea is not None:
        status = "generated" if result.fea.generated else f"FAILED ({result.fea.diagnostic})"
        print(f"CalculiX decks: {status} in {output / 'fea'}")
        if not result.fea.generated:
            return 2
    return 0 if report.overall_pass else 2


def run_fea_evaluate(config_path: Path, output: Path, line_id: str | None = None) -> int:
    """Apply acceptance thresholds to CalculiX results solved from generated decks."""
    from syntera.design import load_run

    _, config, _ = load_run(config_path, line_id)
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


def run_design(config_path: Path, output: Path) -> int:
    """Convert a YAML run configuration into a canonical design document."""
    from syntera.design import DESIGN_FILE, design_from_yaml, design_sha256, save_design

    design = design_from_yaml(config_path)
    target = save_design(design, output / DESIGN_FILE)
    lines = ", ".join(line.id for line in design.lines)
    print(f"Design: {target} (schema version {design.schema_version})")
    print(f"Equipment: {len(design.equipment)}; lines: {lines}")
    print(f"Rationale records: {len(design.rationale)}; sha256: {design_sha256(design)}")
    return 0


def run_route_design(design_path: Path, output: Path) -> int:
    """Route and verify every line of a design document, largest bore first."""
    from syntera.design import load_design_input
    from syntera.multiline import DESIGN_REPORT_FILE, route_design

    report = route_design(load_design_input(design_path), output).report
    print(f"Design: {report.title}")
    print(f"Routing order: {', '.join(report.line_order)} ({report.order_rule})")
    for line in report.lines:
        status = "PASS" if line.overall_pass else "FAIL"
        length = f"{line.route_length_mm:.1f} mm" if line.route_length_mm is not None else "-"
        print(f"- {line.line}: {status}, {length}, {line.bends} bends")
    for pair in report.pairs:
        status = "PASS" if pair.passed else "FAIL"
        print(
            f"- {pair.lines[0]} / {pair.lines[1]}: {status}, "
            f"{pair.minimum_distance_mm:.1f} mm apart ({pair.required_clearance_mm:g} required)"
        )
    print(f"Overall assurance: {'PASS' if report.overall_pass else 'FAIL'}")
    for reason in report.failure_reasons:
        print(f"- {reason}")
    print(f"Report: {output / DESIGN_REPORT_FILE}")
    return 0 if report.overall_pass else 2


def run_change(design_path: Path, changes_path: Path, output: Path, regenerate: bool) -> int:
    """Apply a change set to a design; write the new design, its revert and a diff."""
    from syntera.changes import (
        DIFF_FILE,
        REVERT_FILE,
        ChangeSet,
        apply_change_set,
        diff_designs,
        verdict_changes,
    )
    from syntera.design import DESIGN_FILE, load_design_input, save_design
    from syntera.multiline import route_design

    before = load_design_input(design_path)
    changes = ChangeSet.model_validate_json(changes_path.read_text(encoding="utf-8"))
    after, revert = apply_change_set(before, changes)
    output.mkdir(parents=True, exist_ok=True)
    save_design(after, output / DESIGN_FILE)
    write_json(revert, output / REVERT_FILE)
    diff = diff_designs(before, after)
    status = 0
    if regenerate:
        old = route_design(before, output / "before").report
        new = route_design(after, output / "after").report
        diff = diff.model_copy(update={"verdicts": verdict_changes(old, new), "regenerated": True})
        status = 0 if new.overall_pass else 2
    write_json(diff, output / DIFF_FILE)
    print(f"Applied: {changes.description} ({len(changes.edits)} edit(s))")
    for change in diff.values:
        print(f"- {change.path}: {change.before!r} -> {change.after!r}")
    print(f"Rationale records changed: {len(diff.rationale)}")
    if regenerate:
        for verdict in diff.verdicts:
            print(f"- verdict {verdict.check}: {verdict.before} -> {verdict.after}")
        if not diff.verdicts:
            print("No check changed verdict")
    print(f"Design: {output / DESIGN_FILE}; revert: {output / REVERT_FILE}")
    print(f"Diff: {output / DIFF_FILE}")
    return status


def run_export_dwg(source: Path, target: Path, release: str) -> int:
    """Convert a generated DXF to DWG with the ODA File Converter, when installed."""
    from syntera.drawings.export import export_dwg

    record = export_dwg(source, target, release)
    write_json(record, target.with_suffix(".export.json"))
    print(f"DWG: {target} ({record.release}, written by {record.converter})")
    for warning in record.warnings:
        print(f"- {warning}")
    print(f"Record: {target.with_suffix('.export.json')}")
    return 0


def run_project(step: Path, output: Path) -> int:
    """Hidden-line projection of a STEP assembly into plan, front, right and iso views."""
    from syntera.drawings.projection import (
        PROJECTION_FILE,
        VIEWS_DXF,
        project_shapes,
        step_solids,
        write_projection_dxf,
    )

    projection = project_shapes(step_solids(step))
    output.mkdir(parents=True, exist_ok=True)
    write_json(projection, output / PROJECTION_FILE)
    write_projection_dxf(projection, output / VIEWS_DXF)
    print(f"Projected {len(projection.sources)} solid(s) from {step.name}")
    for view in projection.views:
        print(f"- {view.name}: {view.count(True)} visible, {view.count(False)} hidden polylines")
    print(f"Views: {output / VIEWS_DXF}; data: {output / PROJECTION_FILE}")
    return 0


def run_sheet(run: Path, output: Path, number: str | None) -> int:
    """ISO drawing sheet (frame, title block, scale, views, dimensions) for a run folder."""
    from syntera.drawings.sheet import SHEET_DXF, SHEET_FILE, sheet_from_run

    layout = sheet_from_run(run, output, number)
    choice = layout.choice
    print(f"Sheet: ISO {choice.sheet} at {choice.label} ({layout.drawing_number})")
    print(f"Dimensions: {len(layout.dimensions)}")
    verdict = layout.title_block["ASSURANCE (EXACT GEOMETRY CHECKS)"]
    print(f"Title block assurance (repeated from the report): {verdict}")
    print(f"Sheet: {output / SHEET_DXF}; layout: {output / SHEET_FILE}")
    return 0


def run_drawing_graph(source: Path, output: Path, tag: str | None) -> int:
    """Build the semantic graph of a DXF drawing and optionally look up a tag."""
    from syntera.drawings.graph import (
        GRAPH_FILE,
        dimensions_of,
        find_by_tag,
        graph_from_file,
        label_of,
    )

    graph = graph_from_file(source)
    output.mkdir(parents=True, exist_ok=True)
    write_json(graph, output / GRAPH_FILE)
    attached = sum(label.attached_to is not None for label in graph.labels)
    linked = sum(bool(dimension.measures) for dimension in graph.dimensions)
    print(f"Drawing graph: {len(graph.nodes)} geometry nodes ({graph.space})")
    print(f"Labels attached: {attached} of {len(graph.labels)}")
    print(f"Dimensions linked: {linked} of {len(graph.dimensions)}")
    if graph.title_block:
        print("Title block: " + "; ".join(f"{k}={v}" for k, v in graph.title_block.items()))
    if tag:
        found = find_by_tag(graph, tag)
        print(f"Tag {tag}: {len(found)} entity(ies)")
        for node in found:
            sizes = [f"{d.measurement:g}" for d in dimensions_of(graph, node.handle)
                     if d.measurement is not None]  # fmt: skip
            labels = label_of(graph, node.handle)
            print(
                f"- {node.type} {node.handle} on {node.layer}: labels {labels}, dimensions {sizes}"
            )
    print(f"Graph: {output / GRAPH_FILE}")
    return 0


def run_knowledge(entry_id: str | None) -> int:
    """List the knowledge entries, or show one with its rules and citations."""
    from syntera.knowledge import load_knowledge

    kb = load_knowledge()
    if entry_id is None:
        for entry in kb.entries:
            flag = " (synthetic)" if entry.synthetic else ""
            print(f"{entry.id}: {entry.title}{flag}")
        print(f"{len(kb.entries)} entries")
        return 0
    entry = kb.get(entry_id)
    print(f"{entry.title} [{entry.kind}]")
    print(entry.summary)
    print(entry.detail)
    for rule in entry.rules:
        print(f"- rule {rule}")
    for citation in entry.citations:
        note = f" ({citation.note})" if citation.note else ""
        print(f"- cites [{citation.kind}] {citation.source}{note}")
    return 0


def run_search(query: str, designs: list[Path], graphs: list[Path], limit: int) -> int:
    """Rank knowledge, design rationale and drawing graphs for a query (local BM25)."""
    from syntera.design import load_design_input
    from syntera.knowledge import load_knowledge
    from syntera.knowledge.retrieval import build_index

    rationale = []
    for path in designs:
        design = load_design_input(path)
        rationale.append((path.stem, design.rationale))
    loaded = []
    if graphs:
        from syntera.drawings.graph import DrawingGraph

        for path in graphs:
            loaded.append(
                (path.parent.name or path.stem, DrawingGraph.model_validate_json(path.read_bytes()))
            )
    index = build_index(load_knowledge(), rationale, loaded)
    hits = index.search(query, limit)
    print(f"{len(hits)} result(s) from {len(index)} documents")
    for hit in hits:
        print(f"{hit.score:8.3f}  [{hit.kind}] {hit.title}  ({hit.id})")
    return 0


def run_explain(args: argparse.Namespace) -> int:
    """Explain a design value, or (as inference) a drawing entity or its flow velocity."""
    from syntera.knowledge.explain import (
        back_calculate_velocity,
        explain_entity,
        explain_value,
    )

    if args.design is not None:
        from syntera.design import load_design_input

        if not args.path:
            raise ValueError("--path is required with --design")
        explanation = explain_value(load_design_input(args.design), args.path, args.mode)
    else:
        from syntera.drawings.graph import DrawingGraph

        if args.graph is None or not args.handle:
            raise ValueError("give --design and --path, or --graph and --handle")
        graph = DrawingGraph.model_validate_json(args.graph.read_bytes())
        name = args.graph.parent.name or args.graph.stem
        if args.flow is not None:
            explanation = back_calculate_velocity(
                graph, args.handle, name, args.flow, args.wall, mode=args.mode
            )
        else:
            explanation = explain_entity(graph, args.handle, name, args.mode)
    print(explanation.headline)
    for step in explanation.steps:
        print(f"- {step}")
    for citation in explanation.citations:
        print(f"Source: {citation}")
    for caveat in explanation.caveats:
        print(f"Note: {caveat}")
    return 0


def run_isometrics(run: Path, output: Path) -> int:
    """One isometric sheet per routed line and a bill of materials from the design."""
    from syntera.design import DESIGN_FILE, load_design_input
    from syntera.drawings.isometric import (
        BOM_FILE,
        ISO_PREFIX,
        bill_of_materials,
        write_isometric,
    )

    source = run / DESIGN_FILE if run.is_dir() else run
    design = load_design_input(source)
    bom = bill_of_materials(design)
    if not bom.lines:
        raise ValueError(f"{source}: no routed lines; run demo or route-design first")
    output.mkdir(parents=True, exist_ok=True)
    write_json(bom, output / BOM_FILE)
    for takeoff in bom.lines:
        path = write_isometric(design, takeoff, output / f"{ISO_PREFIX}{takeoff.line}.dxf")
        print(f"Isometric: {path}")
    for row in bom.totals:
        print(f"- {row.item}: {row.size} {row.material} x {row.quantity:g} {row.unit}")
    print(f"Bill of materials: {output / BOM_FILE}")
    return 0


def run_drawing_edit(source: Path, changes_path: Path, output: Path, dwg: bool) -> int:
    """Apply a drawing change set to a DXF, keeping every other entity byte-stable."""
    from syntera.drawings.edit import EDIT_REPORT, EDITED_DXF, DrawingChangeSet, edit_dxf

    changes = DrawingChangeSet.model_validate_json(changes_path.read_text(encoding="utf-8"))
    report = edit_dxf(source, changes, output, dwg)
    write_json(report, output / EDIT_REPORT)
    stable = sum(
        report.fingerprints_before[h] == report.fingerprints_after.get(h)
        for h in report.fingerprints_before
        if h not in report.changed
    )
    print(f"Applied: {changes.description} ({len(changes.edits)} edit(s))")
    print(f"Changed entities: {', '.join(report.changed)}")
    print(f"Untouched entities byte-stable: {stable} of {report.untouched}")
    print(f"Drawing: {output / EDITED_DXF}" + (f"; DWG: {report.dwg}" if report.dwg else ""))
    return 0 if stable == report.untouched else 2


def run_assistant_tools() -> int:
    """Print the assistant tool definitions as JSON schemas."""
    import json

    from syntera.assistant.tools import tool_schemas

    print(json.dumps(tool_schemas(), indent=2, sort_keys=True))
    return 0


def run_ask(design: Path, question: str, output: Path, max_steps: int) -> int:
    """Ask the assistant about a design (needs ANTHROPIC_API_KEY for a real model)."""
    from syntera.assistant.agent import TRANSCRIPT_FILE, provider_from_env, run_agent
    from syntera.assistant.tools import AssistantContext
    from syntera.design import load_design_input

    provider = provider_from_env()
    if provider is None:
        print("No language model is configured: set ANTHROPIC_API_KEY to use ask.",
              file=sys.stderr)  # fmt: skip
        return 2
    ctx = AssistantContext(design=load_design_input(design), output=output)
    result = run_agent(provider, ctx, question, max_steps, transcript_path=output / TRANSCRIPT_FILE)
    print(result.answer)
    print(f"({result.stopped} after {result.steps} step(s); tools: {result.tool_calls})")
    return 0 if result.stopped == "answer" else 2


def run_assistant_eval(mode: str, output: Path) -> int:
    """Run the assistant evaluation suite and report per-question results."""
    from syntera.assistant.evaluation import EVAL_FILE, run_suite

    report = run_suite(output, mode)  # type: ignore[arg-type]
    output.mkdir(parents=True, exist_ok=True)
    write_json(report, output / EVAL_FILE)
    for case in report.cases:
        status = "PASS" if case.passed else "FAIL"
        detail = ""
        if case.missing_facts:
            detail += f" missing facts {case.missing_facts}"
        if case.missing_citations:
            detail += f" missing citations {case.missing_citations}"
        print(f"{status} {case.id}{detail}")
    print(f"{report.passed} of {report.total} questions answered as expected ({report.provider})")
    print(f"Report: {output / EVAL_FILE}")
    return 0 if report.passed == report.total else 2


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
    demo.add_argument(
        "--config", type=Path, required=True, help="YAML config or design.json document"
    )
    demo.add_argument("--output", type=Path, required=True)
    demo.add_argument("--line", help="line id to route when the design has several")
    _add_time_log(demo)
    evaluate = subparsers.add_parser(
        "fea-evaluate", help="apply FEA acceptance thresholds to solved CalculiX results"
    )
    evaluate.add_argument(
        "--config", type=Path, required=True, help="YAML config or design.json document"
    )
    evaluate.add_argument("--output", type=Path, required=True)
    evaluate.add_argument("--line", help="line id when the design has several")
    _add_time_log(evaluate)
    design = subparsers.add_parser(
        "design", help="convert a YAML configuration into a canonical design.json"
    )
    design.add_argument("--config", type=Path, required=True)
    design.add_argument("--output", type=Path, required=True)
    route_all = subparsers.add_parser(
        "route-design", help="route and verify every line of a design, largest bore first"
    )
    route_all.add_argument(
        "--design", type=Path, required=True, help="design.json document or YAML config"
    )
    route_all.add_argument("--output", type=Path, required=True)
    change = subparsers.add_parser(
        "change", help="apply a change set to a design and report the diff"
    )
    change.add_argument("--design", type=Path, required=True)
    change.add_argument("--changes", type=Path, required=True)
    change.add_argument("--output", type=Path, required=True)
    change.add_argument(
        "--regenerate",
        action="store_true",
        help="route and verify the design before and after, and report changed verdicts",
    )
    export = subparsers.add_parser(
        "export-dwg", help="convert a DXF to DWG with the ODA File Converter (if installed)"
    )
    export.add_argument("--input", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--release", default="ACAD2018", help="ODA output version name")
    projecting = subparsers.add_parser(
        "project", help="hidden-line projection of a STEP assembly into a views DXF"
    )
    projecting.add_argument("--step", type=Path, required=True)
    projecting.add_argument("--output", type=Path, required=True)
    sheet = subparsers.add_parser(
        "sheet", help="ISO drawing sheet with views and dimensions for a demo or design run"
    )
    sheet.add_argument("--run", type=Path, required=True, help="demo or route-design output")
    sheet.add_argument("--output", type=Path, required=True)
    sheet.add_argument("--number", help="drawing number for the title block")
    graph = subparsers.add_parser(
        "drawing-graph", help="semantic graph of a DXF: labels, dimensions, title block"
    )
    graph.add_argument("--input", type=Path, required=True)
    graph.add_argument("--output", type=Path, required=True)
    graph.add_argument("--tag", help="look up the entities carrying this tag")
    knowledge = subparsers.add_parser(
        "knowledge", help="list knowledge entries or show one with its citations"
    )
    knowledge.add_argument("--id", dest="entry_id")
    search = subparsers.add_parser(
        "search", help="local keyword search over knowledge, rationale and drawing graphs"
    )
    search.add_argument("--query", required=True)
    search.add_argument("--design", type=Path, action="append", default=[])
    search.add_argument("--graph", type=Path, action="append", default=[])
    search.add_argument("--limit", type=int, default=10)
    explain = subparsers.add_parser(
        "explain", help="explain why a design value is what it is (or infer from a drawing)"
    )
    explain.add_argument("--design", type=Path, help="design.json or YAML config")
    explain.add_argument("--path", help="rationale path, e.g. tube.outer_diameter")
    explain.add_argument("--graph", type=Path, help="drawing_graph.json of a read drawing")
    explain.add_argument("--handle", help="drawing entity handle")
    explain.add_argument("--flow", type=float, help="flow (m3/h) to back-calculate velocity")
    explain.add_argument("--wall", type=float, help="wall thickness (mm) for the bore")
    explain.add_argument("--mode", choices=["plain", "engineer"], default="plain")
    isometrics = subparsers.add_parser(
        "isometrics", help="line isometrics and a bill of materials for a routed design"
    )
    isometrics.add_argument("--run", type=Path, required=True, help="run folder or design.json")
    isometrics.add_argument("--output", type=Path, required=True)
    drawing_edit = subparsers.add_parser(
        "drawing-edit", help="apply a change set to a DXF and write it back (DXF, optional DWG)"
    )
    drawing_edit.add_argument("--input", type=Path, required=True)
    drawing_edit.add_argument("--changes", type=Path, required=True)
    drawing_edit.add_argument("--output", type=Path, required=True)
    drawing_edit.add_argument("--dwg", action="store_true", help="also write DWG (needs ODA)")
    subparsers.add_parser("assistant-tools", help="print the assistant tool JSON schemas")
    evaluation = subparsers.add_parser(
        "assistant-eval", help="run the assistant evaluation suite (scripted or a real model)"
    )
    evaluation.add_argument("--mode", choices=["scripted", "anthropic"], default="scripted")
    evaluation.add_argument("--output", type=Path, default=Path("outputs/assistant-eval"))
    ask = subparsers.add_parser("ask", help="ask the assistant (needs ANTHROPIC_API_KEY)")
    ask.add_argument("--design", type=Path, required=True)
    ask.add_argument("--question", required=True)
    ask.add_argument("--output", type=Path, default=Path("outputs/assistant"))
    ask.add_argument("--max-steps", type=int, default=8)
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
                args.time_log,
                args.task,
                "generate",
                lambda: run_demo(args.config, args.output, args.line),
            )
        if args.command == "fea-evaluate":
            return run_timed(
                args.time_log,
                args.task,
                "fea-check",
                lambda: run_fea_evaluate(args.config, args.output, args.line),
            )
        if args.command == "time":
            return run_time(args.action, args.log, args.task, args.phase, args.note)
        if args.command == "kpi1":
            return run_kpi1(args.log, args.baseline, args.output)
        if args.command == "design":
            return run_design(args.config, args.output)
        if args.command == "route-design":
            return run_route_design(args.design, args.output)
        if args.command == "change":
            return run_change(args.design, args.changes, args.output, args.regenerate)
        if args.command == "export-dwg":
            return run_export_dwg(args.input, args.output, args.release)
        if args.command == "project":
            return run_project(args.step, args.output)
        if args.command == "sheet":
            return run_sheet(args.run, args.output, args.number)
        if args.command == "drawing-graph":
            return run_drawing_graph(args.input, args.output, args.tag)
        if args.command == "knowledge":
            return run_knowledge(args.entry_id)
        if args.command == "search":
            return run_search(args.query, args.design, args.graph, args.limit)
        if args.command == "explain":
            return run_explain(args)
        if args.command == "isometrics":
            return run_isometrics(args.run, args.output)
        if args.command == "drawing-edit":
            return run_drawing_edit(args.input, args.changes, args.output, args.dwg)
        if args.command == "ask":
            return run_ask(args.design, args.question, args.output, args.max_steps)
        if args.command == "assistant-eval":
            return run_assistant_eval(args.mode, args.output)
        if args.command == "assistant-tools":
            return run_assistant_tools()
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
