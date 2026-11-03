"""Optimisation report with trade-offs and sensitivity, and its web view (H3)."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from syntera.cli import main
from syntera.optimise.optimiser import optimise
from syntera.optimise.problem import load_problem
from syntera.optimise.report import MARKDOWN_FILE, REPORT_FILE, build_report, report_markdown
from syntera.web.app import create_app

PROJECT = Path(__file__).parents[2]
EXAMPLE = PROJECT / "examples" / "demo_optimisation.yaml"
FIXED = PROJECT / "examples" / "demo_skid.yaml"
STUDY = {
    "title": "web study",
    "variables": [{"kind": "bend_radius_factor", "line": "main", "low": 1.5, "high": 2.5,
                   "step": 0.5}],
    "objectives": ["length", "bends"],
    "budget": 3,
}  # fmt: skip


def test_report_explains_choice_trade_offs_and_sensitivity(tmp_path):
    problem, design = load_problem(EXAMPLE)
    result = optimise(problem, design, tmp_path / "run")
    report = build_report(problem, result)
    assert report.evaluated == 9 and report.feasible + report.rejected == 9
    assert report.chosen is not None and report.chosen.id == result.chosen
    chosen_row = next(p for p in report.pareto if p.candidate == report.chosen.id)
    assert all(value == 0 for value in chosen_row.deltas.values())
    assert {p.candidate for p in report.pareto} == set(result.pareto)
    for item in report.sensitivity:
        assert item.from_value == report.chosen.values[item.variable]
        assert item.feasible == (item.deltas is not None)
    assert {s.variable for s in report.sensitivity} <= {v.name for v in problem.variables}
    assert report.rationale[0].rule_id == "optimise.knee_point.v1"
    text = report_markdown(report)
    assert f"Candidate {report.chosen.id}" in text and "## Trade-offs" in text
    assert "never traded off" in text


def test_cli_writes_report_files(tmp_path):
    data = EXAMPLE.read_text(encoding="utf-8").replace("budget: 9", "budget: 3")
    data = data.replace("design: demo_skid.yaml", f"design: {FIXED}")
    problem = tmp_path / "p.yaml"
    problem.write_text(data, encoding="utf-8")
    assert main(["optimise", "--problem", str(problem), "--output", str(tmp_path / "o")]) == 0
    report = json.loads((tmp_path / "o" / REPORT_FILE).read_text(encoding="utf-8"))
    assert report["evaluated"] == 3
    assert (tmp_path / "o" / MARKDOWN_FILE).read_text(encoding="utf-8").startswith("# ")


def test_web_runs_a_study_on_the_server_design_and_shows_the_pareto_set(tmp_path):
    client = TestClient(create_app(FIXED, tmp_path / "web"))
    assert client.get("/api/optimise").status_code == 404
    report = client.post("/api/optimise", json=STUDY).json()
    assert report["evaluated"] == 3 and report["objectives"] == ["length", "bends"]
    assert report["chosen"]["id"] in {p["candidate"] for p in report["pareto"]}
    assert client.get("/api/optimise").json() == report
    bad = {**STUDY, "variables": [{**STUDY["variables"][0], "line": "nope"}]}
    assert client.post("/api/optimise", json=bad).status_code == 422
    assert client.post("/api/optimise", json={**STUDY, "budget": 50}).status_code == 422
    assert client.post("/api/optimise", json={**STUDY, "design": "/etc/passwd"}).status_code == 422
    html = client.get("/optimise").text
    assert '<script src="/assets/optimise.js"></script>' in html
    source = client.get("/assets/optimise.js").text
    assert "/api/optimise" in source
    for forbidden in ("eval(", "innerHTML", "new Function"):
        assert forbidden not in source
