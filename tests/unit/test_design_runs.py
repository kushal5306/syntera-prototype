"""Runs derive their configuration from a design document (roadmap A3b)."""

from __future__ import annotations

from pathlib import Path

import pytest

from syntera.design import (
    DesignError,
    design_from_yaml,
    load_design_input,
    load_run,
    rationale_still_applies,
    run_config,
    run_rationale,
    save_design,
    with_route,
)
from syntera.schemas import RouteResult

PROJECT = Path(__file__).parents[2]
FIXED = PROJECT / "examples" / "demo_skid.yaml"
SIZED = PROJECT / "examples" / "demo_skid_sized.yaml"

ROUTE = RouteResult(found=True, points=[(0, 0, 0), (100, 0, 0), (100, 50, 0)], length=150.0)


def test_with_route_records_replaces_and_removes_a_line_route():
    design = design_from_yaml(FIXED)
    routed = with_route(design, "main", ROUTE)
    (record,) = routed.routes
    assert record.line == "main"
    assert record.length_mm == 150.0
    assert [tuple(point) for point in record.points] == ROUTE.points
    assert design.routes == []  # the input document is not changed

    shorter = RouteResult(found=True, points=[(0, 0, 0), (100, 0, 0)], length=100.0)
    assert with_route(routed, "main", shorter).routes[0].length_mm == 100.0
    missing = RouteResult(found=False, diagnostic="no path")
    assert with_route(routed, "main", missing).routes == []
    with pytest.raises(DesignError, match="unknown line"):
        with_route(design, "other", ROUTE)


def test_load_design_input_reads_json_designs_and_converts_yaml(tmp_path):
    design = design_from_yaml(SIZED)
    target = save_design(design, tmp_path / "design.json")
    assert load_design_input(target) == design
    assert load_design_input(SIZED) == design


@pytest.mark.parametrize("source", [FIXED, SIZED], ids=["fixed-od", "sized"])
def test_load_run_gives_the_same_run_from_yaml_and_from_design_json(tmp_path, source):
    design = design_from_yaml(source)
    target = save_design(design, tmp_path / "design.json")
    from_yaml = load_run(source)
    from_json = load_run(target)
    assert from_yaml == from_json
    assert from_json[1] == run_config(design)
    assert from_json[2] == run_rationale(design)
    assert load_run(target, "main") == from_json
    with pytest.raises(DesignError, match="unknown line"):
        load_run(target, "missing")


def test_rationale_is_dropped_for_values_a_client_edited():
    _, config, records = load_run(SIZED)
    assert rationale_still_applies(config, records) == records

    edited = config.model_copy(update={"voxel_resolution": config.voxel_resolution * 2})
    kept = {record.path for record in rationale_still_applies(edited, records)}
    assert "voxel_resolution" not in kept
    assert "tube.outer_diameter" in kept
    # records for inputs outside the run configuration stay with the values they explain
    assert any(path.startswith("line_sizing.") for path in kept)
