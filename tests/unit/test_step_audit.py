from __future__ import annotations

import cadquery as cq

from syntera.geometry.cad import export_step, make_tube
from syntera.pid.build import PLANT_STEP, build_plant
from syntera.pid.dexpi import read_dexpi
from syntera.verification.step_audit import audit_step


def _rules(result):
    return {rule.id: rule for rule in result.audit.rules}


def test_reading_back_a_generated_plant_finds_no_failures(dexpi_xml, tmp_path):
    document = read_dexpi(dexpi_xml.read_bytes(), dexpi_xml.name)
    build_plant(document, tmp_path / "plant", optimise=False)

    result = audit_step(tmp_path / "plant" / PLANT_STEP)

    assert result.equipment == 2 and result.pipes > 3
    assert result.audit.overall_pass, result.audit.summary
    rules = _rules(result)
    # the only loose ends are the inlet and outlet at the skid edge
    assert len(rules["open_ends"].findings) == 2
    ends = sorted(f.location[0] for f in rules["open_ends"].findings)
    assert ends[0] == 0.0 and ends[1] > 1000.0


def test_an_imported_model_with_a_clash_a_gap_and_a_tight_bend_fails(tmp_path):
    vessel = cq.Solid.makeCylinder(500, 2000, cq.Vector(0, 0, 0))
    # nozzle stub stops 10 mm short of the vessel wall
    stub = cq.Solid.makeCylinder(30.15, 300, cq.Vector(510, 0, 1000), cq.Vector(1, 0, 0))
    # a pipe bent at 1 x OD straight through a pump skid
    pipe = make_tube([(810, 0, 1000), (2000, 0, 1000), (2000, 1500, 1000)], 30.15, 60.3)
    pump = cq.Solid.makeBox(400, 400, 400).translate(cq.Vector(1300, -200, 800))
    path = tmp_path / "vendor.step"
    export_step([vessel, stub, pipe, pump], path)

    result = audit_step(path)

    assert (result.pipes, result.equipment) == (2, 2)
    assert result.sizes_mm == (60.3,)
    rules = _rules(result)
    assert not result.audit.overall_pass
    assert rules["clash"].status == "fail"
    assert rules["bend_radius"].findings[0].measured_mm == 60.3
    # the stub touches nothing at its vessel end, so that end is open
    assert any(f.location[0] == 510.0 for f in rules["open_ends"].findings)
