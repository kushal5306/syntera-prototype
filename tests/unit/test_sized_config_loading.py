"""Loading sized configurations from YAML and keeping sizing rationale honest after edits."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from syntera.config import load_config, load_sized_config
from syntera.rules import RuleError, sized_config, sizing_still_applies
from syntera.rules.sizing import TUBE_RULE

PROJECT = Path(__file__).parents[2]
SIZED = PROJECT / "examples" / "demo_skid_sized.yaml"
FIXED = PROJECT / "examples" / "demo_skid.yaml"


def test_load_sized_config_returns_rule_records_for_a_sized_file():
    config, records = load_sized_config(SIZED)
    assert config.tube.outer_diameter == 20
    by_path = {record.path: record for record in records}
    assert by_path["tube.outer_diameter"].source == "rule"
    assert by_path["tube.outer_diameter"].rule_id == TUBE_RULE
    assert by_path["tube.minimum_bend_radius"].source == "rule"
    assert "tube.minimum_clearance" not in by_path  # stated by the user, not sized
    assert load_config(SIZED) == config


def test_load_sized_config_leaves_a_fixed_od_file_unchanged():
    config, records = load_sized_config(FIXED)
    assert records == []
    assert config == load_config(FIXED)
    assert config.tube.outer_diameter == 20


def test_load_sized_config_resolves_step_paths_and_reports_rule_errors(tmp_path):
    raw = yaml.safe_load(SIZED.read_text(encoding="utf-8"))
    raw["obstacles"].append({"type": "step", "name": "vessel", "path": "parts/vessel.step"})
    source = tmp_path / "sized.yaml"
    source.write_text(yaml.safe_dump(raw), encoding="utf-8")
    config, _ = load_sized_config(source)
    step = [obstacle for obstacle in config.obstacles if obstacle.type == "step"][0]
    assert Path(step.path) == (tmp_path / "parts" / "vessel.step").resolve()

    raw["tube"]["outer_diameter"] = 25
    source.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(RuleError, match="either tube.outer_diameter or line_sizing"):
        load_sized_config(source)


def test_sizing_applies_only_while_the_sized_values_are_unchanged():
    sized, records = load_sized_config(SIZED)
    assert records
    same = type(sized).model_validate(sized.model_dump(mode="json"))
    assert sizing_still_applies(sized, same)

    edited_od = same.model_copy(
        update={"tube": same.tube.model_copy(update={"outer_diameter": 25.0})}
    )
    assert not sizing_still_applies(sized, edited_od)

    assert same.analysis is not None
    thicker = same.model_copy(
        update={"analysis": same.analysis.model_copy(update={"wall_thickness": 3.0})}
    )
    assert not sizing_still_applies(sized, thicker)

    without_analysis = same.model_copy(update={"analysis": None})
    assert not sizing_still_applies(sized, without_analysis)

    # changing a load case does not touch any value sizing chose or used
    cases = [case.model_copy() for case in same.analysis.load_cases[:1]]
    fewer_cases = same.model_copy(
        update={"analysis": same.analysis.model_copy(update={"load_cases": cases})}
    )
    assert sizing_still_applies(sized, fewer_cases)


def test_sizing_without_analysis_on_both_sides_still_applies():
    raw = yaml.safe_load(SIZED.read_text(encoding="utf-8"))
    del raw["analysis"]
    sized, _ = sized_config(raw)
    assert sizing_still_applies(sized, sized.model_copy())
