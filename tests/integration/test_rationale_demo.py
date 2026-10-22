from __future__ import annotations

import json
from pathlib import Path

from syntera.config import load_config
from syntera.pipeline import execute_pipeline
from syntera.rationale import RATIONALE_FILE, RationaleReport

PROJECT = Path(__file__).parents[2]


def test_demo_rationale_is_complete_and_deterministic(tmp_path):
    config = load_config(PROJECT / "examples" / "demo_skid.yaml")
    first = execute_pipeline(config, tmp_path / "a", create_preview=False)
    second = execute_pipeline(config, tmp_path / "b", create_preview=False)

    data = (tmp_path / "a" / RATIONALE_FILE).read_bytes()
    assert data == (tmp_path / "b" / RATIONALE_FILE).read_bytes()
    report = RationaleReport.model_validate_json(data)
    assert report == first.rationale == second.rationale
    assert report.schema_version == 1
    assert report.subject == config.title

    # every value the routing run uses from the configuration is explained
    used = {
        "tube.outer_diameter": config.tube.outer_diameter,
        "tube.minimum_bend_radius": config.tube.minimum_bend_radius,
        "tube.minimum_clearance": config.tube.minimum_clearance,
        "voxel_resolution": config.voxel_resolution,
        "routing_cost_weights.bend": config.routing_cost_weights.bend,
        "routing_cost_weights.obstacle_proximity": config.routing_cost_weights.obstacle_proximity,
        "routing_cost_weights.port_direction": config.routing_cost_weights.port_direction,
        "analysis.wall_thickness": config.analysis.wall_thickness,
    }
    for path, value in used.items():
        assert report.get(path).value == value, path
        assert report.get(path).source == "user", path
    paths = [entry["path"] for entry in json.loads(data)["entries"]]
    assert paths == sorted(paths)
    # rationale explains values; it never carries or changes the assurance verdict
    assert "overall_pass" not in data.decode("utf-8")
    assert first.report.overall_pass is True
