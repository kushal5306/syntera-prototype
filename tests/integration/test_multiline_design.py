"""A three-line design routes largest bore first and verifies deterministically (B1)."""

from __future__ import annotations

import json
from pathlib import Path

from syntera.cli import main
from syntera.design import DESIGN_FILE, load_design
from syntera.multiline import DESIGN_REPORT_FILE, LINES_DIR, ORDER_RULE, ROUTED_DESIGN_STEP
from syntera.rationale import RATIONALE_FILE, RationaleReport

PROJECT = Path(__file__).parents[2]
THREE_LINES = PROJECT / "examples" / "demo_three_lines.design.json"


def test_three_line_design_routes_and_verifies_deterministically(tmp_path, capsys):
    first, second = tmp_path / "a", tmp_path / "b"
    assert main(["route-design", "--design", str(THREE_LINES), "--output", str(first)]) == 0
    assert main(["route-design", "--design", str(THREE_LINES), "--output", str(second)]) == 0
    out = capsys.readouterr().out
    assert "Routing order: header, branch, drain" in out
    assert "Overall assurance: PASS" in out

    for name in (DESIGN_REPORT_FILE, DESIGN_FILE):
        assert (first / name).read_bytes() == (second / name).read_bytes(), name
    for line in ("header", "branch", "drain"):
        for name in ("route.json", RATIONALE_FILE):
            path = Path(LINES_DIR) / line / name
            assert (first / path).read_bytes() == (second / path).read_bytes(), path
    assert (first / ROUTED_DESIGN_STEP).is_file()

    report = json.loads((first / DESIGN_REPORT_FILE).read_text(encoding="utf-8"))
    assert report["overall_pass"] is True, report["failure_reasons"]
    assert report["line_order"] == ["header", "branch", "drain"]
    assert [line["overall_pass"] for line in report["lines"]] == [True, True, True]
    # every later line saw more obstacles: earlier pipes replace pending nozzle reserves
    assert len(report["pairs"]) == 3
    assert all(pair["passed"] and not pair["intersects"] for pair in report["pairs"])
    assert all(
        pair["minimum_distance_mm"] >= pair["required_clearance_mm"] for pair in report["pairs"]
    )

    design = load_design(first / DESIGN_FILE)
    assert sorted(route.line for route in design.routes) == ["branch", "drain", "header"]
    assert "overall_pass" not in (first / DESIGN_FILE).read_text(encoding="utf-8")
    for outcome in report["lines"]:
        rationale = RationaleReport.model_validate_json(
            (first / outcome["output"] / RATIONALE_FILE).read_bytes()
        )
        order = rationale.get("routing_order")
        assert order.rule_id == ORDER_RULE and order.value == outcome["order"]


def test_a_line_that_cannot_route_fails_the_whole_design(tmp_path):
    raw = json.loads(THREE_LINES.read_text(encoding="utf-8"))
    # put the drain outlet inside the process vessel: no route can reach it
    for nozzle in raw["nozzles"]:
        if nozzle["id"] == "drain-out":
            nozzle["port"]["position"] = [400, 260, 100]
    source = tmp_path / "broken.design.json"
    source.write_text(json.dumps(raw), encoding="utf-8")
    output = tmp_path / "out"
    assert main(["route-design", "--design", str(source), "--output", str(output)]) == 2
    report = json.loads((output / DESIGN_REPORT_FILE).read_text(encoding="utf-8"))
    assert report["overall_pass"] is False
    assert any(reason.startswith("line drain:") for reason in report["failure_reasons"])
