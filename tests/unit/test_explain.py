"""Deterministic explanations of design values in plain and engineer modes (E3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from syntera.design import design_from_yaml, parse_design
from syntera.knowledge.explain import (
    ASSURANCE_NOTE,
    SYNTHETIC_NOTE,
    ExplainError,
    explain_value,
)
from syntera.rationale import Rationale

PROJECT = Path(__file__).parents[2]
SIZED = design_from_yaml(PROJECT / "examples" / "demo_skid_sized.yaml")
FIXED = design_from_yaml(PROJECT / "examples" / "demo_skid.yaml")


def test_rule_chosen_od_is_explained_from_its_rule_and_knowledge():
    plain = explain_value(SIZED, "tube.outer_diameter")
    assert plain.target == "lines.main.tube.outer_diameter"
    assert plain.headline.startswith("Set by a rule: OD 20 mm is the smallest standard tube")
    assert plain.rule_id == "rules.tube_size_from_flow.v1"
    assert plain.knowledge == ["kb.rule.standard_tube"]
    assert plain.steps[0].startswith("Syntera picks the smallest stocked tube")
    assert SYNTHETIC_NOTE in plain.caveats and plain.caveats[-1] == ASSURANCE_NOTE
    assert not plain.inferred
    assert any(c.startswith("[synthetic]") for c in plain.citations)


def test_engineer_mode_lists_inputs_formulae_and_references():
    eng = explain_value(SIZED, "lines.main.tube.outer_diameter", mode="engineer")
    assert eng.headline == (
        "lines.main.tube.outer_diameter = 20.0 mm (source: rule, rule rules.tube_size_from_flow.v1)"
    )
    assert "flow_m3_per_h = 1.2" in eng.steps and "bore_mm = 16" in eng.steps
    assert any(step.startswith("Reference: Continuity") for step in eng.steps)


def test_bend_radius_names_the_od_it_depends_on():
    bend = explain_value(SIZED, "tube.minimum_bend_radius")
    assert (
        bend.headline
        == "Set by a rule: bend radius 30 mm is 1.5 x the 20 mm OD (long-radius bend)."
    )
    assert bend.related == ["lines.main.tube.outer_diameter"]
    assert bend.knowledge == ["kb.rule.long_radius_bend"]


def test_user_and_default_values_get_background_not_a_rule():
    clearance = explain_value(FIXED, "tube.minimum_clearance")
    assert clearance.source == "user" and clearance.rule_id is None
    assert clearance.steps[0].startswith("Background: ")
    assert clearance.knowledge == ["kb.rule.clearance"]
    weights = explain_value(FIXED, "routing_cost_weights.port_direction")
    assert weights.target == "parameters.routing_cost_weights.port_direction"


def test_explanations_are_deterministic_and_never_carry_a_verdict():
    first = explain_value(SIZED, "tube.outer_diameter", mode="engineer")
    assert first == explain_value(SIZED, "tube.outer_diameter", mode="engineer")
    text = first.model_dump_json().lower()
    assert "overall_pass" not in text and "assurance: pass" not in text


def test_unknown_and_ambiguous_paths_are_refused_helpfully():
    with pytest.raises(ExplainError, match="closest: lines.main.tube.outer_diameter"):
        explain_value(SIZED, "tube outer diam")
    data = SIZED.model_dump(mode="json")
    data["lines"].append({**data["lines"][0], "id": "second"})
    record = Rationale(path="lines.second.tube.outer_diameter", value=20, unit="mm",
                       source="user", explanation="{value} {unit} given.")  # fmt: skip
    data["rationale"] = sorted(
        [*data["rationale"], record.model_dump(mode="json")], key=lambda r: r["path"]
    )
    with pytest.raises(ExplainError, match="ambiguous"):
        explain_value(parse_design(data), "tube.outer_diameter")
