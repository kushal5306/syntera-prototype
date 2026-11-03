"""Change sets: typed edits, exact inverses, diffs and verdict changes (A4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from syntera.changes import (
    AddLine,
    ChangeSet,
    IndexedNozzle,
    MoveEquipment,
    RemoveLine,
    SetParameter,
    apply_change_set,
    diff_designs,
    flatten,
    verdict_changes,
)
from syntera.design import (
    DesignError,
    Line,
    Nozzle,
    RouteRecord,
    design_bytes,
    design_from_yaml,
    parse_design,
)
from syntera.multiline import DesignRunReport, LineOutcome, PairCheck
from syntera.schemas import Port, TubeSpecification

PROJECT = Path(__file__).parents[2]
SIZED = PROJECT / "examples" / "demo_skid_sized.yaml"
FIXED = PROJECT / "examples" / "demo_skid.yaml"
THREE_LINES = PROJECT / "examples" / "demo_three_lines.design.json"
EXAMPLE_CHANGES = PROJECT / "examples" / "demo_skid.changes.json"


def _three_lines():
    return parse_design(json.loads(THREE_LINES.read_text(encoding="utf-8")))


def _round_trip(design, changes):
    changed, revert = apply_change_set(design, changes)
    restored, again = apply_change_set(changed, revert)
    assert design_bytes(restored) == design_bytes(design)
    # reverting the revert reapplies the change
    assert design_bytes(apply_change_set(restored, again)[0]) == design_bytes(changed)
    return changed, revert


def test_set_parameter_records_user_rationale_and_reverts_exactly():
    design = design_from_yaml(SIZED)
    path = "lines.main.tube.outer_diameter"
    assert next(r for r in design.rationale if r.path == path).source == "rule"
    changes = ChangeSet(
        description="larger pipe",
        edits=[SetParameter(path=path, value=25, explanation="matches the {plant} header")],
    )
    changed, _ = _round_trip(design, changes)
    assert changed.line("main").tube.outer_diameter == 25
    record = next(r for r in changed.rationale if r.path == path)
    assert record.source == "user" and record.value == 25 and record.unit == "mm"
    text = record.explanation.format(value=record.value, unit=record.unit)
    assert "matches the {plant} header" in text and "confirmed change set" in text


def test_parameters_title_and_geometry_paths_can_be_set():
    design = design_from_yaml(FIXED)
    changes = ChangeSet(
        description="several values",
        edits=[
            SetParameter(path="parameters.voxel_resolution", value=10),
            SetParameter(path="title", value="Renamed synthetic skid"),
            SetParameter(path="nozzles.end.port.position", value=[560, 220, 100]),
        ],
    )
    changed, _ = _round_trip(design, changes)
    assert changed.parameters.voxel_resolution == 10
    assert changed.title == "Renamed synthetic skid"
    assert changed.nozzle("end").port.position == (560, 220, 100)
    # geometry is the problem statement: no rationale record is made for it
    assert not any(r.path.startswith("nozzles.") for r in changed.rationale)


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("schema_version.x", "can be changed"),
        ("rationale.0", "can be changed"),
        ("lines.main.id", "ids and references"),
        ("lines.main.from_nozzle", "ids and references"),
        ("lines.ghost.tube.outer_diameter", "no item with id ghost"),
        ("lines.main.tube.colour", "no such field"),
        ("lines.main.tube", "not a single value"),
    ],
)
def test_unsafe_or_unknown_paths_are_refused(path, message):
    design = design_from_yaml(FIXED)
    changes = ChangeSet(description="bad", edits=[SetParameter(path=path, value=1)])
    with pytest.raises(DesignError, match=message):
        apply_change_set(design, changes)


def test_an_edit_that_breaks_the_design_is_refused_whole():
    design = design_from_yaml(FIXED)
    changes = ChangeSet(
        description="bend radius below the tube radius",
        edits=[SetParameter(path="lines.main.tube.minimum_bend_radius", value=1)],
    )
    with pytest.raises(DesignError, match="invalid design"):
        apply_change_set(design, changes)


def test_move_equipment_moves_its_nozzles_and_reverts_exactly():
    raw = json.loads(THREE_LINES.read_text(encoding="utf-8"))
    raw["nozzles"][0]["equipment"] = "transfer-pump"
    design = parse_design(raw)
    changes = ChangeSet(
        description="move the pump",
        edits=[MoveEquipment(equipment="transfer-pump", offset=(0.1, -20.3, 0))],
    )
    changed, revert = _round_trip(design, changes)
    pump = next(item for item in changed.equipment if item.id == "transfer-pump")
    assert pump.geometry.center == pytest.approx((600.1, 379.7, 60))
    assert changed.nozzle("branch-in").port.position == pytest.approx((40.1, 379.7, 100))
    assert changed.nozzle("header-in").port.position == design.nozzle("header-in").port.position
    # the inverse restores exact values rather than subtracting the offset
    assert all(edit.op == "set_parameter" for edit in revert.edits)


def test_unknown_equipment_is_reported_with_the_edit_number():
    changes = ChangeSet(
        description="move", edits=[MoveEquipment(equipment="ghost", offset=(1, 0, 0))]
    )
    with pytest.raises(DesignError, match=r"edit 1 \(move_equipment\).*ghost"):
        apply_change_set(design_from_yaml(FIXED), changes)


def test_add_and_remove_lines_round_trip_with_nozzles_and_rationale():
    design = design_from_yaml(SIZED)
    tube = TubeSpecification(outer_diameter=12, minimum_bend_radius=18, minimum_clearance=15)
    new_line = AddLine(
        line=Line(id="vent", from_nozzle="vent-in", to_nozzle="vent-out", tube=tube),
        nozzles=[
            IndexedNozzle(
                nozzle=Nozzle(id="vent-in", port=Port(position=(40, 40, 260), direction=(1, 0, 0)))
            ),
            IndexedNozzle(
                nozzle=Nozzle(
                    id="vent-out", port=Port(position=(560, 40, 260), direction=(-1, 0, 0))
                )
            ),
        ],
    )
    added, _ = _round_trip(design, ChangeSet(description="add vent", edits=[new_line]))
    assert [line.id for line in added.lines] == ["main", "vent"]

    removed, revert = _round_trip(
        added, ChangeSet(description="drop main", edits=[RemoveLine(line="main")])
    )
    assert [line.id for line in removed.lines] == ["vent"]
    assert [nozzle.id for nozzle in removed.nozzles] == ["vent-in", "vent-out"]
    assert not any(r.path.startswith("lines.main.") for r in removed.rationale)
    (restore,) = revert.edits
    assert restore.op == "add_line" and restore.index == 0
    assert all(r.path.startswith("lines.main.") for r in restore.rationale)


def test_lines_and_nozzles_cannot_be_duplicated_or_left_empty():
    design = design_from_yaml(FIXED)
    line = design.line("main")
    with pytest.raises(DesignError, match="already exists"):
        apply_change_set(design, ChangeSet(description="dup", edits=[AddLine(line=line)]))
    with pytest.raises(DesignError, match="invalid design"):
        apply_change_set(design, ChangeSet(description="none", edits=[RemoveLine(line="main")]))
    with pytest.raises(DesignError, match="unknown line"):
        apply_change_set(design, ChangeSet(description="?", edits=[RemoveLine(line="x")]))


def test_stored_routes_are_cleared_and_restored_by_the_revert():
    design = design_from_yaml(FIXED).model_copy(
        update={
            "routes": [
                RouteRecord(line="main", points=[(40, 200, 100), (560, 200, 100)], length_mm=520)
            ]
        }  # fmt: skip
    )
    changes = ChangeSet(
        description="clearance", edits=[SetParameter(path="parameters.voxel_resolution", value=10)]
    )
    changed, revert = _round_trip(design, changes)
    assert changed.routes == []
    assert revert.edits[0].op == "set_routes"
    assert diff_designs(design, changed).routes_changed == ["main"]


def test_change_sets_validate_their_shape():
    with pytest.raises(ValidationError):
        ChangeSet(description="empty", edits=[])
    with pytest.raises(ValidationError):
        ChangeSet.model_validate({"description": "x", "edits": [{"op": "set_verdict"}]})
    example = ChangeSet.model_validate_json(EXAMPLE_CHANGES.read_text(encoding="utf-8"))
    _round_trip(design_from_yaml(FIXED), example)


def test_diff_lists_changed_values_and_rationale_by_stable_paths():
    design = _three_lines()
    changes = ChangeSet(
        description="tweak",
        edits=[
            SetParameter(path="lines.drain.tube.minimum_clearance", value=18),
            MoveEquipment(equipment="process-vessel", offset=(10, 0, 0)),
        ],
    )
    changed, _ = apply_change_set(design, changes)
    diff = diff_designs(design, changed)
    assert [(c.path, c.before, c.after) for c in diff.values] == [
        ("equipment.process-vessel.geometry.center", [400.0, 250.0, 100.0], [410.0, 250.0, 100.0]),
        ("lines.drain.tube.minimum_clearance", 15.0, 18.0),
    ]
    (record,) = diff.rationale
    assert record.path == "lines.drain.tube.minimum_clearance"
    assert record.before is None and record.after.source == "user"
    assert diff_designs(design, design).values == []
    assert "rationale" not in " ".join(flatten(design.model_dump(mode="json")))


def _report(line_pass: dict[str, bool], pairs: dict[tuple[str, str], bool]) -> DesignRunReport:
    lines = [
        LineOutcome(
            line=name,
            order=i,
            outer_diameter_mm=10,
            routed=ok,
            overall_pass=ok,
            route_length_mm=None,
            bends=0,
            obstacles=0,
            output=f"lines/{name}",
            failure_reasons=[],
        )  # fmt: skip
        for i, (name, ok) in enumerate(line_pass.items(), start=1)
    ]
    checks = [
        PairCheck(
            lines=key,
            minimum_distance_mm=50,
            required_clearance_mm=20,
            intersects=not ok,
            passed=ok,
        )  # fmt: skip
        for key, ok in pairs.items()
    ]
    overall = all(line_pass.values()) and all(pairs.values())
    return DesignRunReport(
        title="t", synthetic_data=True, order_rule="r", line_order=list(line_pass),
        lines=lines, pairs=checks, overall_pass=overall, failure_reasons=[],
    )  # fmt: skip


def test_verdict_changes_name_every_check_that_flipped():
    before = _report({"a": True, "b": True}, {("a", "b"): True})
    after = _report({"a": True, "b": False}, {})
    changes = [(c.check, c.before, c.after) for c in verdict_changes(before, after)]
    assert changes == [
        ("line:b", True, False),
        ("overall", True, False),
        ("pair:a/b", True, None),
    ]
    assert verdict_changes(before, before) == []
