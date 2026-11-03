"""A design document converted from YAML drives the same run as the YAML itself."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from syntera.cli import main
from syntera.config import load_sized_config
from syntera.design import DESIGN_FILE, load_design, run_config, run_rationale
from syntera.pipeline import execute_pipeline
from syntera.rationale import RATIONALE_FILE, RationaleReport
from syntera.web.app import create_app

PROJECT = Path(__file__).parents[2]
SIZED = PROJECT / "examples" / "demo_skid_sized.yaml"
BASELINE = PROJECT / "examples" / "baseline_skid.yaml"


def test_design_cli_writes_byte_identical_documents(tmp_path, capsys):
    assert main(["design", "--config", str(SIZED), "--output", str(tmp_path / "a")]) == 0
    assert main(["design", "--config", str(SIZED), "--output", str(tmp_path / "b")]) == 0
    first = (tmp_path / "a" / DESIGN_FILE).read_bytes()
    assert first == (tmp_path / "b" / DESIGN_FILE).read_bytes()
    out = capsys.readouterr().out
    assert "schema version 1" in out
    assert "lines: main" in out
    assert "overall_pass" not in first.decode("utf-8")


def test_run_from_design_matches_run_from_yaml(tmp_path):
    config, records = load_sized_config(SIZED)
    from_yaml = execute_pipeline(config, tmp_path / "yaml", create_preview=False, rationale=records)

    assert main(["design", "--config", str(SIZED), "--output", str(tmp_path / "design")]) == 0
    design = load_design(tmp_path / "design" / DESIGN_FILE)
    from_design = execute_pipeline(
        run_config(design),
        tmp_path / "from-design",
        create_preview=False,
        rationale=run_rationale(design),
    )

    for name in ("route.json", RATIONALE_FILE):
        assert (tmp_path / "yaml" / name).read_bytes() == (
            tmp_path / "from-design" / name
        ).read_bytes(), name
    assert from_design.report.overall_pass == from_yaml.report.overall_pass is True


DETERMINISTIC_OUTPUTS = (
    "route.json",
    RATIONALE_FILE,
    DESIGN_FILE,
    "metrics.json",
    "fea/fea_manifest.json",
    "fea/mesh.inp",
    "fea/design_pressure.inp",
    "fea/self_weight.inp",
    "fea/thermal_rise.inp",
)


def _assurance_without_timing(path: Path) -> dict:
    report = json.loads(path.read_text(encoding="utf-8"))
    report.pop("execution_time_seconds")
    return report


def test_demo_compare_and_fea_from_a_design_document_are_byte_identical(tmp_path):
    assert main(["design", "--config", str(SIZED), "--output", str(tmp_path / "in")]) == 0
    source = tmp_path / "in" / DESIGN_FILE
    runs = [tmp_path / "a", tmp_path / "b"]
    for output in runs:
        assert main(["demo", "--config", str(source), "--output", str(output)]) == 0
        main(["compare", "--baseline", str(BASELINE), "--output", str(output)])

    first, second = runs
    for name in (*DETERMINISTIC_OUTPUTS, "comparison.json"):
        assert (first / name).read_bytes() == (second / name).read_bytes(), name
    assert _assurance_without_timing(first / "assurance_report.json") == (
        _assurance_without_timing(second / "assurance_report.json")
    )

    # The written design is the input plus the route; it never carries a verdict.
    written = load_design(first / DESIGN_FILE)
    given = load_design(source)
    assert written.model_copy(update={"routes": []}) == given
    (route,) = written.routes
    stored = json.loads((first / "route.json").read_text(encoding="utf-8"))
    assert route.length_mm == stored["length"]
    assert "overall_pass" not in (first / DESIGN_FILE).read_text(encoding="utf-8")


def test_demo_outputs_match_from_yaml_and_from_its_design_document(tmp_path):
    assert main(["design", "--config", str(SIZED), "--output", str(tmp_path / "in")]) == 0
    assert main(["demo", "--config", str(SIZED), "--output", str(tmp_path / "yaml")]) == 0
    source = tmp_path / "in" / DESIGN_FILE
    assert main(["demo", "--config", str(source), "--output", str(tmp_path / "json")]) == 0
    for name in DETERMINISTIC_OUTPUTS:
        assert (tmp_path / "yaml" / name).read_bytes() == (tmp_path / "json" / name).read_bytes()


def test_fea_evaluate_accepts_the_design_document(tmp_path):
    assert main(["design", "--config", str(SIZED), "--output", str(tmp_path / "in")]) == 0
    source = tmp_path / "in" / DESIGN_FILE
    output = tmp_path / "demo"
    assert main(["demo", "--config", str(source), "--output", str(output)]) == 0
    if shutil.which("ccx") is None:
        pytest.skip("CalculiX (ccx) is not installed")
    for deck in ("design_pressure", "self_weight", "thermal_rise"):
        subprocess.run(["ccx", "-i", deck], cwd=output / "fea", check=True, capture_output=True)
    # the input hash written with the decks matches the design-derived configuration
    assert main(["fea-evaluate", "--config", str(source), "--output", str(output)]) == 0
    assert main(["compare", "--baseline", str(BASELINE), "--output", str(output)]) == 0


def test_web_serves_a_design_document(tmp_path):
    assert main(["design", "--config", str(SIZED), "--output", str(tmp_path / "in")]) == 0
    client = TestClient(create_app(tmp_path / "in" / DESIGN_FILE, tmp_path / "web"))
    config = client.get("/api/config").json()
    assert config["tube"]["outer_diameter"] == 20
    routed = client.post("/api/route", json=config)
    assert routed.status_code == 200
    report = RationaleReport.model_validate_json(
        (tmp_path / "web" / "latest" / RATIONALE_FILE).read_bytes()
    )
    assert report.get("tube.outer_diameter").source == "rule"

    config["voxel_resolution"] = config["voxel_resolution"] * 2
    assert client.post("/api/route", json=config).status_code == 200
    report = RationaleReport.model_validate_json(
        (tmp_path / "web" / "latest" / RATIONALE_FILE).read_bytes()
    )
    assert report.get("voxel_resolution").value == config["voxel_resolution"]
    assert report.get("voxel_resolution").source == "user"
