from __future__ import annotations

import math
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from syntera.drawings.layout3d import tube_rationale
from syntera.pid.dexpi import read_dexpi
from syntera.pid.layout import place
from syntera.pid.rationale import line_rationale, plant_rationale
from syntera.rationale import (
    Rationale,
    RationaleReport,
    build_report,
    config_rationale,
    grid_rationale,
    model_rationale,
    weights_rationale,
)
from syntera.schemas import DemoConfig, RoutingWeights, TubeSpecification

PROJECT = Path(__file__).parents[2]


def _demo() -> dict:
    return yaml.safe_load((PROJECT / "examples" / "demo_skid.yaml").read_text(encoding="utf-8"))


def test_user_and_default_sources_follow_what_the_input_stated():
    weights = RoutingWeights.model_validate({"bend": 80})
    records = {r.path: r for r in model_rationale(weights, "routing_cost_weights")}
    assert records["routing_cost_weights.bend"].source == "user"
    assert records["routing_cost_weights.bend"].value == 80
    assert records["routing_cost_weights.obstacle_proximity"].source == "default"
    assert records["routing_cost_weights.obstacle_proximity"].value == 5.0
    assert records["routing_cost_weights.bend"].text().startswith("Given by the user: ")
    assert "schema default" in records["routing_cost_weights.port_direction"].text()


@pytest.mark.parametrize("source", ["rule", "calculation"])
def test_rule_and_calculation_must_name_their_rule(source):
    with pytest.raises(ValidationError, match="rule_id"):
        Rationale(path="tube.outer_diameter", value=1.0, source=source, explanation="x")
    record = Rationale(
        path="tube.outer_diameter",
        value=88.9,
        unit="mm",
        source=source,
        rule_id="test.rule.v1",
        inputs={"dn": 80},
        explanation="{value} {unit} for DN{dn}.",
    )
    assert record.text().endswith("88.9 mm for DN80.")


def test_drawing_and_inferred_values_are_labelled_by_source():
    drawn = Rationale(path="a", value=50.0, unit="mm", source="drawing", explanation="{value}")
    inferred = Rationale(
        path="b", value="pump", source="inferred", explanation="looks like a {value}"
    )
    assert drawn.text() == "Read from the drawing: 50"
    assert inferred.text().startswith("Inferred")
    assert inferred.text().endswith("looks like a pump")


def test_explanation_template_must_render_from_its_own_fields():
    with pytest.raises(ValidationError, match="unknown fields"):
        Rationale(path="a", value=1.0, source="user", explanation="{value} from {missing}")
    with pytest.raises(ValidationError):
        Rationale(path="not a path", value=1.0, source="user", explanation="x")
    with pytest.raises(ValidationError):
        Rationale(path="a", value=1.0, source="guess", explanation="x")  # type: ignore[arg-type]


def test_report_paths_are_unique_sorted_and_later_groups_win():
    a = Rationale(path="b.x", value=1.0, source="default", explanation="first")
    b = Rationale(path="a.y", value=2.0, source="user", explanation="second")
    c = Rationale(path="b.x", value=3.0, source="rule", rule_id="r.v1", explanation="override")
    report = build_report("subject", [a, b], [c])
    assert [r.path for r in report.entries] == ["a.y", "b.x"]
    assert report.get("b.x") == c
    with pytest.raises(ValidationError, match="duplicate"):
        RationaleReport(subject="s", entries=[a, a])
    with pytest.raises(ValidationError, match="sorted"):
        RationaleReport(subject="s", entries=[a, b])
    with pytest.raises(KeyError):
        report.get("missing")


def test_report_round_trips_through_json():
    report = build_report(
        "subject",
        [
            grid_rationale(20.0, (600.0, 400.0, 300.0), 40_000),
            Rationale(path="flag", value=True, source="user", explanation="{value}"),
            Rationale(path="count", value=16, source="default", explanation="{value}"),
            Rationale(path="name", value="steel", source="user", explanation="{value}"),
        ],
    )
    again = RationaleReport.model_validate_json(report.model_dump_json())
    assert again == report
    assert again.get("flag").value is True
    assert again.get("voxel_resolution").inputs["workspace_mm"] == (600.0, 400.0, 300.0)


def test_demo_configuration_values_all_have_user_rationale():
    config = DemoConfig.model_validate(_demo())
    report = build_report(config.title, config_rationale(config))
    paths = {r.path for r in report.entries}
    assert {
        "tube.outer_diameter",
        "tube.minimum_bend_radius",
        "tube.minimum_clearance",
        "voxel_resolution",
        "routing_cost_weights.bend",
        "routing_cost_weights.obstacle_proximity",
        "routing_cost_weights.port_direction",
        "analysis.wall_thickness",
        "analysis.material.yield_strength_mpa",
        "analysis.acceptance.stress_safety_factor",
        "analysis.acceptance.maximum_displacement_mm",
        "analysis.mesh.circumferential_elements",
    } <= paths
    # the problem statement (geometry) is input, not a chosen value
    assert not any(
        p.startswith(("workspace", "obstacles", "start_port", "end_port")) for p in paths
    )
    assert report.get("tube.outer_diameter").value == 20
    assert report.get("tube.outer_diameter").unit == "mm"
    assert report.get("analysis.material.yield_strength_mpa").unit == "MPa"
    # every field present in the YAML is the user's; the unset mesh length is skipped
    assert {r.source for r in report.entries} == {"user"}
    assert "analysis.mesh.maximum_axial_element_length" not in paths


def test_omitted_settings_are_reported_as_defaults():
    data = _demo()
    del data["routing_cost_weights"]
    del data["analysis"]["acceptance"]["stress_safety_factor"]
    report = build_report("s", config_rationale(DemoConfig.model_validate(data)))
    assert report.get("routing_cost_weights.bend").source == "default"
    assert report.get("routing_cost_weights.bend").value == 20.0
    assert report.get("analysis.acceptance.stress_safety_factor").source == "default"
    assert report.get("tube.outer_diameter").source == "user"


def test_grid_and_weight_rules_record_their_inputs():
    grid = grid_rationale(250.0, (9000.0, 6000.0, 4500.0), 120_000)
    assert grid.source == "calculation"
    assert "120000 grid nodes" in grid.text()
    bend, proximity, port = weights_rationale(1000.0, 250.0, 1000.0, 250.0, "pid.w.v1")
    assert bend.inputs["factor"] == 4.0 and proximity.inputs["factor"] == 1.0
    assert "never relaxes the clearance check" in proximity.text()
    assert port.source == "rule" and "hard constraint" in port.text()


def test_pid_line_rationale_matches_the_plant_rules():
    records = {r.path: r for r in line_rationale(80, 88.9, 250.0)}
    od = records["tube.outer_diameter"]
    assert (od.source, od.value, od.rule_id) == (
        "rule",
        88.9,
        "pid.outside_diameter_from_nominal_size.v1",
    )
    assert "DN80" in od.text() and od.references
    assert records["tube.minimum_bend_radius"].value == pytest.approx(133.35)
    assert records["tube.minimum_clearance"].value == pytest.approx(44.45)
    assert records["routing_cost_weights.bend"].value == 1000.0
    small = {r.path: r for r in line_rationale(15, 21.3, 250.0)}
    assert small["tube.minimum_clearance"].value == 25.0
    unstated = {r.path: r for r in line_rationale(None, 60.3, 250.0)}
    assert unstated["tube.outer_diameter"].source == "default"
    assert "no nominal size" in unstated["tube.outer_diameter"].text()


def test_pid_plant_rationale_explains_sizes_and_positions(dexpi_xml):
    document = read_dexpi(dexpi_xml.read_bytes(), dexpi_xml.name)
    layout = place(document, 250.0)
    report = build_report("plant", plant_rationale(document, layout, 250.0))
    tank, pump = report.get("equipment.T100.size"), report.get("equipment.P101.size")
    assert tank.source == "calculation"  # diameter from capacity over length
    assert tank.rule_id == "pid.vessel_diameter_from_capacity.v1"
    assert tank.value[0] == pytest.approx(math.sqrt(4 * 2e9 / (math.pi * 2000)))
    assert pump.source == "default" and "class default" in pump.text()
    assert report.get("equipment.P101.center").source == "calculation"
    assert report.get("plant.workspace").value == layout.workspace
    assert report == build_report("plant", plant_rationale(document, place(document, 250.0), 250.0))


def test_drawing_tube_rationale_names_user_drawing_and_rule_sources():
    tube = TubeSpecification(outer_diameter=60.3, minimum_bend_radius=90.45, minimum_clearance=30)
    records = {
        r.path: r
        for r in tube_rationale(
            tube,
            od_source="drawing",
            radius_source="default",
            radius_clamped=False,
            clearance_source="user",
        )
    }
    assert records["tube.outer_diameter"].source == "drawing"
    bend = records["tube.minimum_bend_radius"]
    assert (bend.source, bend.rule_id) == ("rule", "drawing.bend_radius_long_radius.v1")
    assert "1.5 x the 60.3 mm OD" in bend.text()
    assert records["tube.minimum_clearance"].source == "user"

    clamped = tube_rationale(
        TubeSpecification(outer_diameter=60, minimum_bend_radius=30, minimum_clearance=30),
        od_source="default",
        radius_source="drawing",
        radius_clamped=True,
        clearance_source="default",
    )
    by_path = {r.path: r for r in clamped}
    assert by_path["tube.outer_diameter"].source == "default"
    assert by_path["tube.minimum_bend_radius"].rule_id == (
        "drawing.bend_radius_at_least_tube_radius.v1"
    )
    assert by_path["tube.minimum_clearance"].rule_id == "drawing.clearance_half_od.v1"
