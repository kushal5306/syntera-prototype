from __future__ import annotations

import math

import cadquery as cq
import pytest

from syntera.geometry.cad import make_tube
from syntera.verification.assembly_audit import AuditPart, PipeEnd, audit_assembly
from syntera.verification.measure import measure_tube

OD = 60.3


def _box(name: str, low: tuple[float, float, float], size: tuple[float, float, float]):
    shape = cq.Solid.makeBox(*size).translate(cq.Vector(*low))
    return AuditPart(name=name, role="equipment", shape=shape)


def _unit(a, b):
    return tuple((b[i] - a[i]) / math.dist(a, b) for i in range(3))


def _pipe(name: str, points, bend: float = 1.5 * OD, minimum: float | None = None, **extra):
    shape = make_tube(points, OD / 2, bend)
    first = _unit(points[0], points[1])
    last = _unit(points[-1], points[-2])
    return AuditPart(
        name=name,
        role="pipe",
        shape=shape,
        outer_diameter=OD,
        minimum_bend_radius=minimum if minimum is not None else 1.5 * OD,
        clearance=OD / 2,
        junctions=(points[0], points[-1]),
        ends=(PipeEnd(points[0], first), PipeEnd(points[-1], last)),
        **extra,
    )


def _stub(name: str, start, end, owner: str | None):
    direction = cq.Vector(*(end[i] - start[i] for i in range(3)))
    shape = cq.Solid.makeCylinder(
        OD / 2, direction.Length, cq.Vector(*start), direction.normalized()
    )
    return AuditPart(
        name=name, role="stub", shape=shape, outer_diameter=OD, clearance=OD / 2,
        junctions=(start, end), owner=owner,
    )  # fmt: skip


def _clean() -> list[AuditPart]:
    """Two tanks joined by an L-shaped pipe with a nozzle stub at each end."""
    return [
        _box("T1", (0, 0, 0), (1000, 1000, 1000)),
        _box("T2", (3000, 2000, 0), (1000, 1000, 1000)),
        _stub("nozzle T1 N1", (1000, 500, 500), (1200, 500, 500), "T1"),
        _stub("nozzle T2 N1", (3500, 2000, 500), (3500, 1800, 500), "T2"),
        _pipe("line 1", [(1200, 500, 500), (3500, 500, 500), (3500, 1800, 500)]),
    ]


def _rule(audit, rule_id):
    return next(rule for rule in audit.rules if rule.id == rule_id)


def test_clean_assembly_passes_every_rule():
    audit = audit_assembly(_clean())
    assert audit.overall_pass, audit.summary
    assert {rule.status for rule in audit.rules} == {"pass"}
    assert _rule(audit, "bend_radius").checked == 1
    assert _rule(audit, "connections").checked == 4  # two stubs on equipment, two pipe ends


def test_pipe_through_equipment_is_a_clash_with_its_location():
    parts = [*_clean(), _box("P1", (2000, 300, 300), (600, 400, 400))]
    audit = audit_assembly(parts)
    clash = _rule(audit, "clash")
    assert clash.status == "fail" and not audit.overall_pass
    (finding,) = clash.findings
    assert set(finding.parts) == {"line 1", "P1"}
    assert finding.location[0] == pytest.approx(2300, abs=1)


def test_clearance_is_measured_between_unjoined_parts():
    # 20 mm gap from the pipe surface to a box; the line needs OD/2 = 30 mm
    top = 500 + OD / 2 + 20
    parts = [*_clean(), _box("cable tray", (2000, 0, top), (400, 1000, 50))]
    audit = audit_assembly(parts)
    (finding,) = _rule(audit, "clearance").findings
    assert finding.measured_mm == pytest.approx(20, abs=1e-3)
    assert finding.required_mm == pytest.approx(OD / 2)


def test_bend_radius_is_read_from_the_solid_not_the_specification():
    parts = _clean()
    parts[-1] = _pipe(
        "line 1", [(1200, 500, 500), (3500, 500, 500), (3500, 1800, 500)], bend=OD, minimum=1.5 * OD
    )
    audit = audit_assembly(parts)
    (finding,) = _rule(audit, "bend_radius").findings
    assert finding.measured_mm == pytest.approx(OD, abs=1e-6)
    assert measure_tube(parts[-1].shape).bend_radii == pytest.approx((OD,))


def test_nozzle_off_the_equipment_and_open_pipe_end_fail_connections():
    parts = _clean()
    parts[2] = _stub("nozzle T1 N1", (1015, 500, 500), (1200, 500, 500), "T1")  # 15 mm gap
    del parts[3]  # T2 nozzle stub missing: the pipe ends in mid air
    audit = audit_assembly(parts)
    messages = [f.message for f in _rule(audit, "connections").findings]
    assert any("stops 15.0 mm short of T1" in m for m in messages)
    assert any("nothing joined to it" in m for m in messages)


def test_pipes_sharing_a_nozzle_may_meet_only_at_the_joint():
    parts = _clean()
    # a second line leaving the same port along the same axis runs inside the first one
    parts.append(_pipe("line 2", [(1200, 500, 500), (2500, 500, 500), (2500, -500, 500)]))
    audit = audit_assembly(parts)
    (finding,) = _rule(audit, "clash").findings
    assert "overlap beyond it" in finding.message

    parts[-1] = _stub("tee", (1200, 500, 500), (1200, 500, 750), None)  # a square branch
    assert _rule(audit_assembly(parts), "clash").status == "pass"


def test_missing_lines_and_detours_are_reported():
    parts = _clean()
    pipe = parts[-1]
    parts[-1] = AuditPart(**{**pipe.__dict__, "direct_distance": 2000.0, "length": 3600.0})
    audit = audit_assembly(parts, missing_lines=["line 9 (A to B)"])
    assert _rule(audit, "completeness").status == "fail"
    detour = _rule(audit, "detour")
    assert detour.status == "advisory" and "1.8x" in detour.findings[0].message
    assert "1 item(s) to review" in audit.summary
