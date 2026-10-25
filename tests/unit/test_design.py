"""Canonical design document: conversion, validation, persistence and migration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from syntera import design as design_module
from syntera.config import load_sized_config
from syntera.design import (
    DESIGN_SCHEMA_VERSION,
    DesignDocument,
    DesignError,
    config_path,
    design_bytes,
    design_from_config,
    design_from_yaml,
    design_path,
    design_sha256,
    load_design,
    migrate,
    parse_design,
    run_config,
    run_rationale,
    save_design,
)
from syntera.rationale import build_report, config_rationale
from syntera.rules.sizing import TUBE_RULE
from syntera.schemas import DemoConfig

PROJECT = Path(__file__).parents[2]
FIXED = PROJECT / "examples" / "demo_skid.yaml"
SIZED = PROJECT / "examples" / "demo_skid_sized.yaml"


def _raw(design: DesignDocument) -> dict:
    return json.loads(design_bytes(design))


@pytest.mark.parametrize("source", [FIXED, SIZED], ids=["fixed-od", "sized"])
def test_yaml_converts_and_derives_the_same_run_configuration(source):
    config, records = load_sized_config(source)
    design = design_from_yaml(source)

    assert design.schema_version == DESIGN_SCHEMA_VERSION
    assert [item.id for item in design.equipment] == [item.name for item in config.obstacles]
    assert [nozzle.id for nozzle in design.nozzles] == ["start", "end"]
    assert run_config(design) == config
    # The pipeline would write the same rationale.json from the design as from the YAML.
    expected = build_report(config.title, config_rationale(config), records)
    derived = run_config(design)
    assert build_report(config.title, config_rationale(derived), run_rationale(design)) == expected


def test_sized_design_keeps_the_duty_and_rule_rationale():
    design = design_from_yaml(SIZED)
    line = design.line()
    assert line.line_sizing is not None
    assert line.line_sizing.flow_m3_per_h == 1.2
    od = next(r for r in design.rationale if r.path == "lines.main.tube.outer_diameter")
    assert od.source == "rule"
    assert od.rule_id == TUBE_RULE
    assert od.value == line.tube.outer_diameter
    bend = next(r for r in design.rationale if r.path == "parameters.routing_cost_weights.bend")
    assert bend.source == "user"

    fixed = design_from_yaml(FIXED)
    assert fixed.line().line_sizing is None
    od = next(r for r in fixed.rationale if r.path == "lines.main.tube.outer_diameter")
    assert od.source == "user"


def test_inputs_are_hashed_and_missing_step_files_refused(tmp_path):
    design = design_from_yaml(FIXED)
    assert [(item.role, item.path) for item in design.inputs] == [("config", "demo_skid.yaml")]
    assert len(design.inputs[0].sha256) == 64

    raw = yaml.safe_load(FIXED.read_text(encoding="utf-8"))
    raw["obstacles"].append({"type": "step", "name": "vessel", "path": "parts/vessel.step"})
    source = tmp_path / "with_step.yaml"
    source.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(DesignError, match="STEP file .* not found"):
        design_from_yaml(source)

    (tmp_path / "parts").mkdir()
    (tmp_path / "parts" / "vessel.step").write_bytes(b"ISO-10303-21; synthetic placeholder")
    hashed = design_from_yaml(source)
    assert [(item.role, item.path) for item in hashed.inputs] == [
        ("config", "with_step.yaml"),
        ("step", "vessel.step"),
    ]


def test_save_and_load_round_trip_byte_identically(tmp_path):
    design = design_from_yaml(SIZED)
    first = save_design(design, tmp_path / "a" / "design.json")
    second = save_design(design_from_yaml(SIZED), tmp_path / "b" / "design.json")
    assert first.read_bytes() == second.read_bytes()
    assert first.read_bytes().endswith(b"\n")

    loaded = load_design(first)
    assert loaded == design
    assert design_bytes(loaded) == first.read_bytes()
    assert design_sha256(loaded) == design_sha256(design)
    assert run_config(loaded) == run_config(design)

    changed = design.model_copy(update={"title": "Another title"})
    assert design_sha256(changed) != design_sha256(design)


def test_rationale_paths_map_between_design_and_run_configuration():
    assert design_path("voxel_resolution", "main") == "parameters.voxel_resolution"
    assert design_path("routing_cost_weights.bend", "l1") == "parameters.routing_cost_weights.bend"
    assert design_path("tube.outer_diameter", "l1") == "lines.l1.tube.outer_diameter"
    assert design_path("sizing.flow_velocity", "l1") == "lines.l1.sizing.flow_velocity"
    assert config_path("lines.l1.tube.outer_diameter", "l1") == "tube.outer_diameter"
    assert config_path("lines.l2.tube.outer_diameter", "l1") is None
    assert config_path("parameters.voxel_resolution", "l1") == "voxel_resolution"


def test_documents_with_broken_references_are_refused():
    raw = _raw(design_from_yaml(FIXED))

    bad_nozzle = json.loads(json.dumps(raw))
    bad_nozzle["lines"][0]["to_nozzle"] = "missing"
    with pytest.raises(ValidationError, match="unknown nozzle missing"):
        parse_design(bad_nozzle)

    duplicate = json.loads(json.dumps(raw))
    duplicate["equipment"].append(duplicate["equipment"][0])
    with pytest.raises(ValidationError, match="duplicate equipment ids"):
        parse_design(duplicate)

    renamed = json.loads(json.dumps(raw))
    renamed["equipment"][0]["id"] = "other"
    with pytest.raises(ValidationError, match="geometry name must equal"):
        parse_design(renamed)

    outside = json.loads(json.dumps(raw))
    outside["nozzles"][0]["port"]["position"] = [-1, 0, 0]
    with pytest.raises(ValidationError, match="outside the workspace"):
        parse_design(outside)

    unsorted = json.loads(json.dumps(raw))
    unsorted["rationale"].reverse()
    with pytest.raises(ValidationError, match="sorted by path"):
        parse_design(unsorted)

    stray = json.loads(json.dumps(raw))
    stray["rationale"][0]["path"] = "tube.outer_diameter"
    stray["rationale"].sort(key=lambda record: record["path"])
    with pytest.raises(ValidationError, match="must start with parameters"):
        parse_design(stray)

    unknown_route = json.loads(json.dumps(raw))
    unknown_route["routes"] = [{"line": "l9", "points": [[0, 0, 0], [1, 0, 0]], "length_mm": 1}]
    with pytest.raises(ValidationError, match="route for unknown line"):
        parse_design(unknown_route)

    unknown_field = json.loads(json.dumps(raw))
    unknown_field["assurance"] = {"overall_pass": True}
    with pytest.raises(ValidationError, match="Extra inputs"):
        parse_design(unknown_field)


def test_unknown_line_and_ambiguous_line_are_reported():
    design = design_from_yaml(FIXED)
    with pytest.raises(DesignError, match="unknown line other"):
        design.line("other")
    raw = _raw(design)
    raw["lines"].append({**raw["lines"][0], "id": "second"})
    two = parse_design(raw)
    with pytest.raises(DesignError, match="2 lines"):
        run_config(two)
    assert run_config(two, "second").tube == design.line().tube


def test_invalid_json_and_non_object_documents_are_refused(tmp_path):
    broken = tmp_path / "design.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(DesignError, match="not valid JSON"):
        load_design(broken)
    with pytest.raises(DesignError, match="JSON object"):
        parse_design([1, 2])


def test_migration_refuses_unknown_versions():
    current = _raw(design_from_yaml(FIXED))
    assert migrate(current) == current
    with pytest.raises(DesignError, match="newer than this Syntera supports"):
        migrate({**current, "schema_version": DESIGN_SCHEMA_VERSION + 1})
    with pytest.raises(DesignError, match="no integer schema_version"):
        migrate({k: v for k, v in current.items() if k != "schema_version"})
    with pytest.raises(DesignError, match="no integer schema_version"):
        migrate({**current, "schema_version": True})
    with pytest.raises(DesignError, match="no migration from design schema_version 0"):
        migrate({**current, "schema_version": 0})


def test_registered_migrations_upgrade_one_version_at_a_time(monkeypatch):
    current = _raw(design_from_yaml(FIXED))

    def from_v0(raw: dict) -> dict:
        upgraded = {key: value for key, value in raw.items() if key != "name"}
        upgraded["title"] = raw["name"]
        upgraded["schema_version"] = 1
        return upgraded

    monkeypatch.setitem(design_module.MIGRATIONS, 0, from_v0)
    legacy = {k: v for k, v in current.items() if k != "title"}
    legacy.update(schema_version=0, name=current["title"])
    assert parse_design(legacy) == parse_design(current)

    monkeypatch.setitem(design_module.MIGRATIONS, 0, lambda raw: dict(raw))
    with pytest.raises(DesignError, match="did not upgrade it"):
        migrate(legacy)


def test_design_from_config_records_override_config_defaults(base_data):
    config = DemoConfig.model_validate(base_data)
    design = design_from_config(config)
    weights = next(r for r in design.rationale if r.path == "parameters.routing_cost_weights.bend")
    assert weights.source == "user"
    unstated = DemoConfig.model_validate(
        {k: v for k, v in base_data.items() if k != "routing_cost_weights"}
    )
    default = next(
        r
        for r in design_from_config(unstated).rationale
        if r.path == "parameters.routing_cost_weights.bend"
    )
    assert default.source == "default"
    # Defaults survive a save/load cycle even though the JSON states every value.
    reloaded = parse_design(_raw(design_from_config(unstated)))
    records = {r.path: r for r in run_rationale(reloaded)}
    assert records["routing_cost_weights.bend"].source == "default"
