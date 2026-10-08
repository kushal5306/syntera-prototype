from __future__ import annotations

import json
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from syntera.cli import run_demo
from syntera.config import load_config
from syntera.web.app import create_app

PROJECT_ROOT = Path(__file__).parents[2]


def write_step_config(directory: Path, step_name: str) -> Path:
    """Copy the demo skid, replacing the analytical vessel with imported STEP geometry."""
    data = yaml.safe_load((PROJECT_ROOT / "examples" / "demo_skid.yaml").read_text("utf-8"))
    data["obstacles"][0] = {
        "type": "step",
        "name": "imported-process-vessel",
        "path": step_name,
        "translation": [300, 200, 120],
        "defeaturing": {"max_feature_radius": 10},
    }
    path = directory / "step_skid.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_step_obstacle_demo_routes_and_reports_import(tmp_path, vessel_step):
    config_path = write_step_config(tmp_path, vessel_step.name)
    output = tmp_path / "demo"
    assert run_demo(config_path, output) == 0
    assert {
        "assembly.step",
        "routed_assembly.step",
        "route.json",
        "assurance_report.json",
        "route_preview.png",
        "import_report.json",
    } == {path.name for path in output.iterdir()}

    report = json.loads((output / "assurance_report.json").read_text(encoding="utf-8"))
    assert report["overall_pass"] is True
    assert report["minimum_measured_clearance_mm"] >= report["required_clearance_mm"]

    imported = json.loads((output / "import_report.json").read_text(encoding="utf-8"))
    (summary,) = imported["obstacles"]
    assert summary["name"] == "imported-process-vessel"
    assert summary["source_path"] == str(vessel_step.resolve())
    assert summary["removed_features"] >= 1
    assert summary["routing_volume_mm3"] > summary["original_volume_mm3"]
    assert summary["routing_bounding_box_mm"] == [[250, 120, 30], [350, 280, 210]]


def test_step_route_matches_equivalent_box(tmp_path, vessel_step):
    """With rounds and bore filled, the vessel occupies its box envelope at this resolution."""
    step_route = json.loads(
        (_run(write_step_config(tmp_path, vessel_step.name), tmp_path / "step")).read_text("utf-8")
    )
    box_route = json.loads(
        _run(PROJECT_ROOT / "examples" / "demo_skid.yaml", tmp_path / "box").read_text("utf-8")
    )
    assert step_route["points"] == box_route["points"]


def _run(config: Path, output: Path) -> Path:
    assert run_demo(config, output) == 0
    return output / "route.json"


def test_web_rejects_step_paths_outside_server_config(tmp_path, vessel_step):
    config_path = write_step_config(tmp_path, vessel_step.name)
    client = TestClient(create_app(config_path, tmp_path / "web"))
    config = client.get("/api/config").json()
    assert config["obstacles"][0]["path"] == str(vessel_step.resolve())
    assert client.post("/api/preview", json=config).status_code == 200

    config["obstacles"][0]["path"] = str(tmp_path / "elsewhere.step")
    response = client.post("/api/route", json=config)
    assert response.status_code == 422
    assert "not listed in the server configuration" in response.json()["detail"]


def test_relative_step_path_resolves_against_config(tmp_path, vessel_step):
    config = load_config(write_step_config(tmp_path, vessel_step.name))
    assert config.obstacles[0].path == str(vessel_step.resolve())
