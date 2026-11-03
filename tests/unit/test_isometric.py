"""Bill of materials and line isometrics from the design document (C3)."""

from __future__ import annotations

import json
from pathlib import Path

import ezdxf
import pytest

from syntera.components.tables import flange_for
from syntera.design import parse_design
from syntera.drawings.isometric import bill_of_materials, iso_point, take_off, write_isometric
from syntera.drawings.trace import trace_dxf

PROJECT = Path(__file__).parents[2]
THREE_LINES = PROJECT / "examples" / "demo_three_lines.design.json"
HEADER = [[40, 100, 100], [300, 100, 100], [300, 100, 260], [500, 100, 260], [500, 100, 100],
          [760, 100, 100]]  # fmt: skip
BRANCH = [[40, 400, 100], [300, 400, 100], [300, 400, 300], [760, 400, 300]]


def _design(header_points=HEADER):
    raw = json.loads(THREE_LINES.read_text(encoding="utf-8"))
    for line in raw["lines"]:
        if line["id"] == "header":
            line["fittings"] = {"rating": "PN16", "end_connection": "flanged"}
    raw["routes"] = [
        {"line": "branch", "points": BRANCH, "length_mm": 980.0},
        {"line": "header", "points": header_points, "length_mm": 1040.0},
    ]
    return parse_design(raw)


def test_bom_quantities_match_the_design_model():
    design = _design()
    bom = bill_of_materials(design)
    assert [takeoff.line for takeoff in bom.lines] == ["branch", "header"]
    for takeoff in bom.lines:
        route = next(r for r in design.routes if r.line == takeoff.line)
        line = design.line(takeoff.line)
        rows = {row.item: row for row in takeoff.rows}
        bends = len(route.points) - 2
        elbows = rows.get("elbow_lr") or rows.get("pipe_bend")
        assert elbows.quantity == bends
        flanged = line.fittings is not None
        assert rows.get("flange") is None if not flanged else rows["flange"].quantity == 2
        pipe = rows["pipe"]
        assert pipe.quantity == pytest.approx(sum(pipe.lengths_mm))
        thickness = flange_for(line.tube.outer_diameter, "PN16").thickness if flanged else 0
        arcs = bends * line.tube.minimum_bend_radius * 3.141592653589793 / 2
        assert pipe.quantity + arcs + 2 * thickness == pytest.approx(
            takeoff.centreline_mm, abs=1e-3
        )
        assert takeoff.run_lengths_mm == pytest.approx(
            [sum(abs(a - b) for a, b in zip(p, q, strict=True))
             for p, q in zip(route.points, route.points[1:], strict=False)]
        )  # fmt: skip
        assert all(row.material == "unspecified" for row in takeoff.rows)
    header = {row.item: row for row in bom.lines[1].rows}
    assert header["elbow_lr"].size == "OD 30 R 45"
    assert header["flange"].size == "OD 30 PN16"
    branch = {row.item: row for row in bom.lines[0].rows}
    assert "flange" not in branch  # welded ends


def test_totals_add_lines_of_the_same_item_and_size():
    raw = json.loads(THREE_LINES.read_text(encoding="utf-8"))
    for line in raw["lines"]:
        line["tube"] = {"outer_diameter": 20, "minimum_bend_radius": 30, "minimum_clearance": 20}
    raw["routes"] = [
        {"line": "branch", "points": BRANCH, "length_mm": 980.0},
        {"line": "header", "points": HEADER, "length_mm": 1040.0},
    ]
    bom = bill_of_materials(parse_design(raw))
    totals = {row.item: row for row in bom.totals}
    per_line = [{row.item: row for row in takeoff.rows} for takeoff in bom.lines]
    assert totals["elbow_lr"].quantity == sum(rows["elbow_lr"].quantity for rows in per_line)
    assert totals["pipe"].quantity == pytest.approx(sum(r["pipe"].quantity for r in per_line))
    assert len(totals["pipe"].lengths_mm) == sum(len(r["pipe"].lengths_mm) for r in per_line)
    assert totals["pipe"].line == "all"


def test_unrouted_lines_are_not_taken_off():
    design = _design()
    with pytest.raises(ValueError, match="no stored route"):
        take_off(design, "drain")
    assert "drain" not in [t.line for t in bill_of_materials(design).lines]


def test_isometric_projection_and_sheet(tmp_path):
    assert iso_point((0, 0, 10)) == (0.0, 10.0)
    x, y = iso_point((100, 0, 0))
    assert x > 0 and y < 0
    design = _design()
    takeoff = take_off(design, "header")
    path = write_isometric(design, takeoff, tmp_path / "iso_header.dxf")
    doc = ezdxf.readfile(path)
    assert not doc.audit().has_errors
    texts = [t.dxf.text for t in doc.modelspace().query("TEXT")]
    assert "260" in texts and "160" in texts and "200" in texts  # run lengths corner to corner
    assert "LINE header - BILL OF MATERIALS" in texts
    report = trace_dxf(path, design)
    assert report.untagged == [] and report.unresolved == []
    assert report.by_kind["line"] > 0
