"""A piping general arrangement drawn the way real ones are: line numbers, EL labels, risers."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from syntera.drawings.annotations import parse_elevation, parse_line_number
from syntera.drawings.intake import DXF_FILE, ingest_drawing
from syntera.drawings.layout3d import infer_layout
from syntera.drawings.models import AssemblyOptions
from syntera.drawings.piping_check import ASSEMBLY_DIR, PLANT_STEP, check_drawing
from syntera.drawings.reader import drawing_units, load_dxf

PROJECT = Path(__file__).parents[2]


@pytest.fixture(scope="module")
def plant_room(tmp_path_factory) -> Path:
    spec = importlib.util.spec_from_file_location(
        "plant_room_drawing", PROJECT / "examples" / "plant_room_drawing.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build(tmp_path_factory.mktemp("plant") / "plant-room-piping-ga.dxf")


@pytest.fixture(scope="module")
def layout(plant_room):
    doc, _, _ = load_dxf(plant_room)
    return infer_layout(doc, drawing_units(doc).mm_per_unit, AssemblyOptions())


def test_line_numbers_give_the_size():
    assert parse_line_number("100-P-2001-A1A").outer_diameter_mm == 114.3
    assert parse_line_number('4"-P-2001-A1A').dn == 100
    assert parse_line_number("2-CW-101").dn == 50  # bare small number is NPS
    assert parse_line_number("DN150-P-2010").outer_diameter_mm == 168.3
    assert parse_line_number("SECTION A-A") is None
    assert parse_line_number("T-201") is None


def test_elevations_read_metres_millimetres_and_datum():
    assert parse_elevation("CL EL +4.200").value_mm == 4200.0
    assert parse_elevation("EL +4200").value_mm == 4200.0
    assert parse_elevation("EL 3,75").value_mm == 3750.0
    bottom = parse_elevation("BOP EL +3.000")
    assert bottom.datum == "bottom" and bottom.centreline(88.9) == pytest.approx(3044.45)
    assert parse_elevation("FFL EL +0.000") is None
    assert parse_elevation("ELEVATION") is None


def test_views_equipment_and_structure_come_from_the_plan_and_section(layout):
    assert layout.supported
    assert layout.views == ["plan", "front"]
    assert layout.workspace == (14400.0, 10400.0, 6000.0)
    assert sorted(e.name for e in layout.equipment) == [
        "E-220", "P-201A", "P-201B", "P-230", "R-210", "T-201", "T-202", "V-230",
    ]  # fmt: skip
    assert all(e.height_source == "drawing" for e in layout.equipment)
    by_name = {o.name: o for o in layout.obstacles}
    condenser = by_name["E-220"]
    assert condenser.center[2] - condenser.size[2] / 2 == pytest.approx(2400.0)
    assert len(layout.structures) == 11  # 9 building columns and 2 condenser posts
    post = by_name["S-STEEL at 10500, 3850"]
    assert post.size[2] == pytest.approx(2400.0)  # from the section, not floor to roof


def test_runs_follow_their_elevations_risers_and_nozzles(layout):
    lines = {line.name: line for line in layout.lines}
    assert len(lines) == 14
    discharge = lines["80-P-2003-A1A"]
    assert discharge.tube.outer_diameter == 88.9
    # up from the pump top nozzle, across at EL 4200, down onto the still's top head
    assert discharge.points[0] == (5200.0, 7100.0, 1050.0)
    assert discharge.points[1] == (5200.0, 7100.0, 4200.0)
    assert discharge.points[-1] == (9800.0, 7600.0, 3850.0)
    assert [(e.kind, e.equipment) for e in discharge.ends] == [
        ("nozzle", "P-201A"), ("nozzle", "R-210"),
    ]  # fmt: skip
    assert discharge.orthogonal and not discharge.problems
    nitrogen = lines["25-N-2041-N1A"]
    assert {(x, y) for x, y, _ in nitrogen.points} >= {(800.0, 3200.0)}
    assert (800.0, 3200.0, 4400.0) in nitrogen.points  # riser where the labels change
    assert (800.0, 3200.0, 4600.0) in nitrogen.points
    assert [e.kind for e in lines["100-P-2002-A1A"].ends] == ["tee", "nozzle"]
    assert [e.kind for e in lines["50-CW-2030-C1A"].ends] == ["nozzle", "battery_limit"]
    assert [e.kind for e in lines["80-P-2020-A1A"].ends] == ["battery_limit", "nozzle"]


def test_drawn_plant_audit_finds_the_seeded_clash(plant_room, tmp_path):
    summary = ingest_drawing(plant_room.read_bytes(), plant_room.name, tmp_path)
    doc, _, _ = load_dxf(tmp_path / summary.drawing_id / DXF_FILE)
    report = check_drawing(
        doc, summary, tmp_path / summary.drawing_id, AssemblyOptions(optimise=False)
    ).report
    rules = {rule.id: rule for rule in report.audit.rules}

    assert not report.audit.overall_pass
    assert sorted(f.parts for f in rules["clash"].findings) == [
        ["80-P-2003-A1A", "150-P-2010-B1A"],
        # the cooling water lines leave through the wall where column C-2 stands
        ["S-COLS at 14200, 5200", "50-CW-2030-C1A"],
        ["S-COLS at 14200, 5200", "50-CW-2031-C1A"],
    ]
    # the condensate line's elbow does not fit under the condenser's bottom nozzle
    assert [f.parts for f in rules["completeness"].findings] == [["80-P-2011-B1A"]]
    condensate = next(line for line in report.lines if line.name == "80-P-2011-B1A")
    assert "leg 4 is 50 mm long but its bends need 133 mm" in condensate.drawn.failure_reasons[0]
    for rule in ("connections", "bend_radius", "outside_diameter", "access"):
        assert rules[rule].status == "pass"
    # one traceable issue per error, located in the drawing's own coordinates
    assert report.drawing_origin_mm == (-200.0, -200.0)
    issues = {tuple(issue.parts): issue for issue in report.issues}
    crossing = issues[("80-P-2003-A1A", "150-P-2010-B1A")]
    assert crossing.rule == "clash"
    assert (crossing.where.x, crossing.where.y) == (9600.0, 7600.0)
    assert crossing.where.el == pytest.approx(4200.0, abs=1.0)
    assert crossing.evidence.required_mm == 25.0
    assert "another elevation" in crossing.fix
    elbow = issues[("80-P-2011-B1A",)]
    assert elbow.evidence.measured_mm == 50.0
    assert elbow.evidence.required_mm == pytest.approx(133.35, abs=0.1)
    assert (elbow.where.x, elbow.where.y) == (13000.0, 3650.0)
    assert len(report.issues) == 4
    assert all(line.route_rationale is None for line in report.lines)
    assert all(
        line.optimised.failure_reasons[0].startswith("not requested") for line in report.lines
    )
    assert (tmp_path / summary.drawing_id / ASSEMBLY_DIR / PLANT_STEP).stat().st_size > 0
