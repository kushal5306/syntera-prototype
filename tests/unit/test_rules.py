"""Rule library: hand-calculated cases, versioning, determinism and config sizing.

Hand calculations (kept here so a reviewer can redo them):

* bore for 1.2 m3/h at 2 m/s: Q = 1.2 / 3600 = 3.3333e-4 m3/s, A = Q / v = 1.6667e-4 m2,
  d = sqrt(4 A / pi) = 0.014567 m = 14.567 mm.
* velocity of 1.2 m3/h in an 18 mm bore: A = pi 0.018^2 / 4 = 2.5447e-4 m2,
  v = 3.3333e-4 / 2.5447e-4 = 1.3099 m/s.
* hoop wall at 10 MPa, OD 20 mm, S 138 MPa, E 1: t = 10 x 20 / (2 x 138) = 0.7246 mm;
  with E 0.85 and c 0.5 mm: 200 / 234.6 + 0.5 = 1.3525 mm.
* demo_skid_sized.yaml: allowable 205 / 1.5 = 136.6667 MPa; with the stated 2 mm wall,
  OD 16 leaves a 12 mm bore (< 14.567 mm) and OD 20 a 16 mm bore, so OD 20 is chosen at
  v = 3.3333e-4 / (pi 0.016^2 / 4) = 1.6579 m/s.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from syntera.rationale import build_report, config_rationale
from syntera.rules import RULE_LIBRARY_VERSION, RULES, RuleError, size_line, sized_config
from syntera.rules.line import LineSizingSpec
from syntera.rules.sizing import (
    TUBE_RULE,
    WALL_RULE,
    allowable_stress_from_yield,
    bend_radius,
    clearance,
    flow_velocity,
    maximum_velocity,
    minimum_inner_diameter,
    minimum_wall_thickness,
    select_material,
    select_tube,
)
from syntera.rules.tables import (
    MATERIAL_BANDS,
    MEDIUM_LIMITS,
    SYNTHETIC_TABLE_NOTICE,
    TUBE_SIZES,
    TubeSize,
)
from syntera.schemas import Material

PROJECT = Path(__file__).parents[2]


def _sized_demo() -> dict:
    text = (PROJECT / "examples" / "demo_skid_sized.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(text)


# ---- individual rules ------------------------------------------------------------------


def test_minimum_bore_matches_hand_calculation():
    result = minimum_inner_diameter(1.2, 2.0)
    assert result.value == pytest.approx(14.567, abs=1e-3)
    record = result.records[0]
    assert record.source == "calculation" and record.unit == "mm"
    assert "1.2 m3/h" in record.text() and "2 m/s" in record.text()


def test_flow_velocity_matches_hand_calculation():
    assert flow_velocity(1.2, 18).value == pytest.approx(1.3099, abs=1e-4)


def test_hoop_wall_matches_hand_calculation():
    assert minimum_wall_thickness(10, 20, 138).value == pytest.approx(0.7246, abs=1e-4)
    derated = minimum_wall_thickness(10, 20, 138, joint_efficiency=0.85, corrosion_allowance_mm=0.5)
    assert derated.value == pytest.approx(1.3525, abs=1e-4)
    assert derated.records[0].rule_id == WALL_RULE
    assert "0.5 mm corrosion allowance" in derated.records[0].text()
    assert minimum_wall_thickness(0, 20, 138).value == 0


def test_bend_and_clearance_rules():
    assert bend_radius(20).value == 30
    assert bend_radius(20, 3).value == 60
    assert clearance(20).value == 25  # floor applies
    assert clearance(80).value == 40  # half the OD
    assert clearance(20, 5).value == 10


def test_medium_and_material_tables():
    assert maximum_velocity("water").value == 2.0
    assert maximum_velocity("steam").records[0].references == (SYNTHETIC_TABLE_NOTICE,)
    cool = select_material("water", 40)
    assert cool.value.material.name == "stainless_316"
    assert cool.value.allowable_stress_mpa == 138
    hot = select_material("water", 150)
    assert hot.value.allowable_stress_mpa == 120  # next band, lower allowable
    assert {r.path for r in cool.records} == {"sizing.material", "sizing.allowable_stress"}
    with pytest.raises(RuleError, match="no material in the synthetic table for water at 250"):
        select_material("water", 250)


def test_allowable_stress_from_user_material():
    steel = Material(name="steel", elastic_modulus_mpa=200000, poisson_ratio=0.3,
                     yield_strength_mpa=240)  # fmt: skip
    result = allowable_stress_from_yield(steel, 1.5)
    assert result.value == 160
    assert result.records[0].source == "calculation"
    with pytest.raises(RuleError):
        allowable_stress_from_yield(steel, 0.9)


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda: minimum_inner_diameter(0, 2), "positive"),
        (lambda: flow_velocity(1, 0), "positive"),
        (lambda: minimum_wall_thickness(-1, 20, 138), "pressure"),
        (lambda: minimum_wall_thickness(1, 20, 138, joint_efficiency=0), "efficiency"),
        (lambda: minimum_wall_thickness(1, 20, 138, corrosion_allowance_mm=-1), "corrosion"),
        (lambda: bend_radius(20, 0.4), "factor"),
        (lambda: clearance(0), "positive"),
    ],
)
def test_rules_refuse_invalid_inputs(call, message):
    with pytest.raises(RuleError, match=message):
        call()


# ---- tube selection ------------------------------------------------------------------


def test_tube_is_smallest_size_meeting_velocity_and_wall():
    result = select_tube(1.2, 2.0, 10, 138)
    choice = result.value
    # OD 16 / wall 1.0 leaves a 14 mm bore (< 14.567 mm); OD 20 / wall 1.0 leaves 18 mm.
    assert (choice.outer_diameter, choice.wall_thickness, choice.inner_diameter) == (20, 1.0, 18)
    assert choice.velocity == pytest.approx(1.3099, abs=1e-4)
    assert choice.velocity <= 2.0
    primary = result.records[0]
    assert primary.path == "tube.outer_diameter" and primary.rule_id == TUBE_RULE
    assert "smallest standard tube" in primary.text()
    paths = [r.path for r in result.records]
    assert len(paths) == len(set(paths))


def test_higher_pressure_needs_thicker_wall_and_larger_tube():
    # At 40 MPa OD 20 needs 2.899 mm: no stocked wall, so OD 25 (min 3.6232 mm) is also
    # refused and OD 30 needs 4.3478 mm (stocks up to 3.0); OD 38 needs 5.5072 (up to 4.0);
    # OD 50 needs 7.2464 (up to 5.0). Nothing fits.
    with pytest.raises(RuleError, match="no standard tube carries") as raised:
        select_tube(1.2, 2.0, 40, 138)
    assert "OD 20: no wall >= 2.8986 mm" in str(raised.value)
    thick = select_tube(1.2, 2.0, 25, 138).value
    # 25 MPa: OD 20 needs 1.8116 -> wall 2.0 -> bore 16 mm, which meets 14.567 mm.
    assert (thick.outer_diameter, thick.wall_thickness) == (20, 2.0)


def test_stated_wall_is_used_and_refused_when_too_thin():
    stated = select_tube(1.2, 2.0, 10, 136.6667, wall_thickness_mm=2.0)
    assert stated.value.outer_diameter == 20 and stated.value.inner_diameter == 16
    assert stated.value.velocity == pytest.approx(1.6579, abs=1e-4)
    wall_record = next(r for r in stated.records if r.path == "sizing.wall_thickness")
    assert wall_record.source == "user" and wall_record.rule_id is None
    with pytest.raises(RuleError, match="no wall >= "):
        select_tube(1.2, 2.0, 10, 138, wall_thickness_mm=0.1)


def test_tube_selection_ignores_table_order():
    shuffled = tuple(reversed(TUBE_SIZES))
    assert select_tube(1.2, 2.0, 10, 138, sizes=shuffled) == select_tube(1.2, 2.0, 10, 138)
    custom = (TubeSize(outer_diameter=22, walls=(1.0,)),)
    assert select_tube(1.2, 2.0, 10, 138, sizes=custom).value.outer_diameter == 22


# ---- library versioning ---------------------------------------------------------------


def test_every_rule_is_versioned_and_registered():
    assert RULE_LIBRARY_VERSION == 1
    assert len(RULES) == len(set(RULES)) == 9
    for rule_id in RULES:
        assert re.fullmatch(r"rules\.[a-z0-9_]+\.v[0-9]+", rule_id), rule_id


def test_tables_are_synthetic_sorted_and_complete():
    ods = [size.outer_diameter for size in TUBE_SIZES]
    assert ods == sorted(ods) and len(ods) == len(set(ods))
    for size in TUBE_SIZES:
        assert list(size.walls) == sorted(size.walls)
        assert all(2 * wall < size.outer_diameter for wall in size.walls)
    media = {limit.medium for limit in MEDIUM_LIMITS}
    assert media == {band.medium for band in MATERIAL_BANDS}
    assert "Synthetic" in SYNTHETIC_TABLE_NOTICE and "not" in SYNTHETIC_TABLE_NOTICE


# ---- sizing a configuration -------------------------------------------------------------


def test_size_line_chooses_everything_from_duty():
    spec = LineSizingSpec(medium="water", flow_m3_per_h=1.2, design_pressure_mpa=10,
                          design_temperature_c=40)  # fmt: skip
    sizing = size_line(spec)
    assert sizing.tube.outer_diameter == 20
    assert sizing.minimum_bend_radius == 30
    assert sizing.minimum_clearance == 25
    assert sizing.material.name == "stainless_316"
    assert sizing.allowable_stress_mpa == 138
    assert all(r.source in {"rule", "calculation"} for r in sizing.records)


def test_user_velocity_limit_replaces_table_limit():
    spec = LineSizingSpec(medium="water", flow_m3_per_h=1.2, design_pressure_mpa=10,
                          design_temperature_c=40, maximum_velocity_m_per_s=1.0)  # fmt: skip
    sizing = size_line(spec)
    # bore >= sqrt(4 x 3.3333e-4 / pi) = 20.601 mm -> OD 25 / wall 1.5 leaves 22 mm.
    assert (sizing.tube.outer_diameter, sizing.tube.inner_diameter) == (25, 22)
    assert all(r.path != "sizing.maximum_velocity" for r in sizing.records)


def test_sized_demo_config_takes_od_from_rules_and_keeps_stated_values():
    config, records = sized_config(_sized_demo())
    assert config.tube.outer_diameter == 20
    assert config.tube.minimum_bend_radius == 30  # rule: 1.5 x OD
    assert config.tube.minimum_clearance == 20  # stated by the user
    assert config.analysis is not None and config.analysis.wall_thickness == 2

    report = build_report(config.title, config_rationale(config), records)
    assert report.get("tube.outer_diameter").source == "rule"
    assert report.get("tube.outer_diameter").rule_id == TUBE_RULE
    assert report.get("tube.minimum_bend_radius").source == "rule"
    assert report.get("tube.minimum_clearance").source == "user"
    assert report.get("analysis.wall_thickness").source == "user"
    assert report.get("analysis.material.name").source == "user"
    assert report.get("line_sizing.flow_m3_per_h").source == "user"
    assert report.get("line_sizing.flow_m3_per_h").unit == "m3/h"
    assert report.get("line_sizing.design_temperature_c").unit == "degC"
    assert report.get("line_sizing.joint_efficiency").source == "default"
    assert report.get("sizing.flow_velocity").value == pytest.approx(1.6579, abs=1e-4)
    assert report.get("sizing.allowable_stress").value == pytest.approx(136.6667, abs=1e-4)
    for entry in report.entries:
        entry.text()  # every record renders


def test_sized_config_fills_wall_and_material_when_not_stated():
    raw = _sized_demo()
    del raw["analysis"]["wall_thickness"], raw["analysis"]["material"], raw["tube"]
    config, records = sized_config(raw)
    assert config.analysis is not None
    assert config.analysis.wall_thickness == 1.0  # thinnest stocked wall >= 0.7246 mm
    assert config.analysis.material.name == "stainless_316"
    assert config.tube.minimum_clearance == 25
    report = build_report(config.title, config_rationale(config), records)
    for path in (
        "analysis.wall_thickness",
        "analysis.material.name",
        "analysis.material.yield_strength_mpa",
        "tube.minimum_clearance",
    ):
        assert report.get(path).source == "rule", path
    assert "material table" in report.get("analysis.material.yield_strength_mpa").text()


def test_sized_config_without_analysis_and_without_sizing():
    raw = _sized_demo()
    del raw["analysis"]
    config, records = sized_config(raw)
    assert config.analysis is None and config.tube.outer_diameter == 20
    assert all(not r.path.startswith("analysis.") for r in records)

    fixed = yaml.safe_load((PROJECT / "examples" / "demo_skid.yaml").read_text("utf-8"))
    config, records = sized_config(fixed)
    assert config.tube.outer_diameter == 20 and records == []


def test_sized_config_refuses_two_sources_and_unsafe_walls():
    raw = _sized_demo()
    raw["tube"]["outer_diameter"] = 25
    with pytest.raises(RuleError, match="either tube.outer_diameter or line_sizing"):
        sized_config(raw)
    thin = _sized_demo()
    thin["analysis"]["wall_thickness"] = 0.2  # below the 0.7317 mm minimum at OD 20
    with pytest.raises(RuleError, match="no standard tube carries"):
        sized_config(thin)
    bad = _sized_demo()
    bad["line_sizing"]["medium"] = "slurry"
    with pytest.raises(ValidationError):
        sized_config(bad)


def test_sizing_is_deterministic_and_does_not_mutate_input():
    raw = _sized_demo()
    before = json.dumps(raw, sort_keys=True)
    first = sized_config(raw)
    second = sized_config(_sized_demo())
    assert json.dumps(raw, sort_keys=True) == before
    assert first[0] == second[0]
    dump = [r.model_dump(mode="json") for r in first[1]]
    assert dump == [r.model_dump(mode="json") for r in second[1]]
