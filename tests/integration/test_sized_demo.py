"""The demo, compare, FEA and web runs size a line from flow and explain it in rationale.json."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from syntera.cli import main, run_demo
from syntera.rationale import RATIONALE_FILE, RationaleReport
from syntera.rules.sizing import TUBE_RULE
from syntera.web.app import create_app

PROJECT = Path(__file__).parents[2]
SIZED = PROJECT / "examples" / "demo_skid_sized.yaml"
BASELINE = PROJECT / "examples" / "baseline_skid.yaml"


def test_sized_demo_routes_verifies_and_explains_the_rule_chosen_od(tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    assert run_demo(SIZED, first) == 0
    assert run_demo(SIZED, second) == 0

    data = (first / RATIONALE_FILE).read_bytes()
    assert data == (second / RATIONALE_FILE).read_bytes()
    report = RationaleReport.model_validate_json(data)
    od = report.get("tube.outer_diameter")
    assert od.source == "rule" and od.rule_id == TUBE_RULE and od.value == 20
    assert report.get("tube.minimum_bend_radius").source == "rule"
    assert report.get("tube.minimum_clearance").source == "user"
    assert report.get("line_sizing.flow_m3_per_h").value == 1.2
    assert "overall_pass" not in data.decode("utf-8")

    assurance = json.loads((first / "assurance_report.json").read_text(encoding="utf-8"))
    assert assurance["overall_pass"] is True
    manifest = json.loads((first / "fea" / "fea_manifest.json").read_text(encoding="utf-8"))
    assert manifest["generated"] is True


def test_sized_demo_compares_and_evaluates_fea_from_the_same_sizing(tmp_path):
    output = tmp_path / "demo"
    assert run_demo(SIZED, output) == 0
    args = ["compare", "--baseline", str(BASELINE), "--output", str(output)]
    if shutil.which("ccx") is None:
        assert main(args) == 2  # unsolved FEA is never counted
        report = json.loads((output / "comparison.json").read_text(encoding="utf-8"))
        assert report["design_valid"] is False
        return
    for deck in ("design_pressure", "self_weight", "thermal_rise"):
        subprocess.run(["ccx", "-i", deck], cwd=output / "fea", check=True, capture_output=True)
    # fea-evaluate re-sizes the line from the same file, so the input hash matches
    assert main(["fea-evaluate", "--config", str(SIZED), "--output", str(output)]) == 0
    assert main(args) == 0


def test_web_reports_sizing_until_the_browser_edits_a_sized_value(tmp_path):
    client = TestClient(create_app(SIZED, tmp_path / "web"))
    config = client.get("/api/config").json()
    assert config["tube"]["outer_diameter"] == 20
    assert "line_sizing" not in config  # the browser only receives the validated config

    routed = client.post("/api/route", json=config)
    assert routed.status_code == 200
    assert routed.json()["assurance"]["overall_pass"] is True
    rationale = tmp_path / "web" / "latest" / RATIONALE_FILE
    report = RationaleReport.model_validate_json(rationale.read_bytes())
    assert report.get("tube.outer_diameter").source == "rule"

    config["tube"]["minimum_clearance"] = 22
    assert client.post("/api/route", json=config).status_code == 200
    report = RationaleReport.model_validate_json(rationale.read_bytes())
    assert report.get("tube.outer_diameter").source == "user"
    assert report.get("tube.minimum_clearance").value == 22
    assert all(not entry.path.startswith("line_sizing.") for entry in report.entries)
