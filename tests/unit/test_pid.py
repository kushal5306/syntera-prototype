from __future__ import annotations

import json
import math

import pytest

from syntera.pid.build import PLANT_STEP, REPORT_FILE, build_plant
from syntera.pid.dexpi import PidError, read_dexpi
from syntera.pid.layout import envelope, outside_diameter, place


def test_dexpi_connectivity_passes_through_inline_items_and_drops_blind_stubs(dexpi_xml):
    document = read_dexpi(dexpi_xml.read_bytes(), dexpi_xml.name)

    assert [e.tag for e in document.equipment] == ["T100", "P101"]
    runs = {(c.line, c.start.label, c.end.label): c for c in document.connections}
    assert set(runs) == {
        ("L1", "line L1 inlet at skid edge", "T100 N1"),
        ("L2", "P101 N1", "T100 N2"),
        ("L3", "P101 N2", "line L3 outlet at skid edge"),
    }
    l2 = runs[("L2", "P101 N1", "T100 N2")]
    assert l2.nominal_diameter == 80
    assert l2.via == ["PipeTee-1", "BallValve-1"]  # BlindFlange-1 branch dropped
    assert document.skipped == ["Frame-1 (Skid) has no nozzles; not placed"]


def test_dexpi_rejects_other_xml_and_entity_declarations():
    with pytest.raises(PidError, match="PlantModel"):
        read_dexpi(b"<svg/>", "x.xml")
    with pytest.raises(PidError, match="document type"):
        read_dexpi(b'<!DOCTYPE x [<!ENTITY a "b">]><PlantModel/>', "x.xml")


def test_sizes_come_from_the_pid_else_stated_class_defaults(dexpi_xml):
    document = read_dexpi(dexpi_xml.read_bytes(), dexpi_xml.name)
    tank, pump = document.equipment
    tank_env = envelope(tank)
    assert tank_env.source == "pid"
    assert tank_env.size[0] == pytest.approx(math.sqrt(4 * 2e9 / (math.pi * 2000)))
    assert envelope(pump).source == "default"
    assert outside_diameter(80) == 88.9
    assert outside_diameter(None) == 60.3


def test_layout_is_deterministic_and_ports_sit_on_the_grid(dexpi_xml):
    document = read_dexpi(dexpi_xml.read_bytes(), dexpi_xml.name)
    first, second = place(document, 200.0), place(document, 200.0)
    assert first == second
    for port in first.ports.values():
        assert all(abs(v / 200.0 - round(v / 200.0)) < 1e-9 for v in port.port)
        assert sum(abs(v) for v in port.direction) == 1.0


def test_plant_routes_and_verifies_every_line(dexpi_xml, tmp_path):
    document = read_dexpi(dexpi_xml.read_bytes(), dexpi_xml.name)
    result = build_plant(document, tmp_path / "plant")
    report = result.report

    assert report.routed == 3 and report.failed == 0
    assert all(route.assurance_pass for route in report.routes)
    assert report.total_pipe_length_mm == pytest.approx(
        sum(route.length_mm for route in report.routes)
    )
    # pipes do not clash with each other: each later line saw earlier ones as obstacles
    tubes = [g.tube for g in result.routes]
    for i, a in enumerate(tubes):
        for b in tubes[i + 1 :]:
            assert a.intersect(b).Volume() <= 1e-6
    saved = json.loads((tmp_path / "plant" / REPORT_FILE).read_text())
    assert saved["routed"] == 3
    assert (tmp_path / "plant" / PLANT_STEP).is_file()
    assert (tmp_path / "plant" / "route-1" / "config.json").is_file()
