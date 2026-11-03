"""ISO drawing sheets: sheet and scale rule, placement, dimensions and title block (C2)."""

from __future__ import annotations

import cadquery as cq
import ezdxf
import pytest

from syntera.drawings.projection import ProjectedView, Projection, project_shapes
from syntera.drawings.sheet import (
    SCALES,
    SHEET_RULE,
    SheetInput,
    SheetLabel,
    SheetRoute,
    arrangement_size,
    choose_sheet,
    drawing_area,
    layout_sheet,
    to_view,
    write_sheet_dxf,
)


def _view(name, size, direction, x_direction):
    return ProjectedView(
        name=name, direction=direction, x_direction=x_direction,
        bounds=((0.0, 0.0), size), polylines=[],
    )  # fmt: skip


def _boxy(x, y, z) -> Projection:
    iso = (x + y, x + y + z)
    return Projection(
        sources=["s"],
        deflection_mm=0.05,
        views=[
            _view("plan", (x, y), (0, 0, 1), (1, 0, 0)),
            _view("front", (x, z), (0, -1, 0), (1, 0, 0)),
            _view("right", (y, z), (1, 0, 0), (0, 1, 0)),
            _view("isometric", iso, (1, -1, 1), (1, 1, 0)),
        ],
    )


def _sheet(verdict=True, nozzles=(), routes=()) -> SheetInput:
    return SheetInput(
        title="Synthetic test skid", drawing_number="SYN-TEST", verdict=verdict,
        verdict_source="assurance_report.json", design_sha256="ab" * 32, synthetic_data=True,
        nozzles=list(nozzles), routes=list(routes),
    )  # fmt: skip


def _fits(projection, sheet, scale):
    width, height = arrangement_size(projection, scale[0] / scale[1])
    area = drawing_area(sheet)
    return width <= area[0] and height <= area[1]


@pytest.mark.parametrize("size", [(10, 10, 10), (600, 400, 300), (3000, 1500, 2000)])
def test_choice_is_the_largest_fitting_scale_on_the_smallest_sheet(size):
    projection = _boxy(*size)
    choice = choose_sheet(projection)
    assert _fits(projection, choice.sheet, choice.scale)
    larger = [scale for scale in SCALES if scale[0] / scale[1] > choice.factor]
    assert not any(_fits(projection, choice.sheet, scale) for scale in larger)


def test_tiny_parts_are_enlarged_and_huge_ones_fall_back_below_one_to_ten():
    tiny = choose_sheet(_boxy(10, 10, 10))
    assert (tiny.sheet, tiny.scale, tiny.preferred) == ("A3", (2, 1), True)
    huge = choose_sheet(_boxy(60000, 30000, 20000))
    # nothing fits A3; A2 is the smallest sheet that fits at all, at its largest scale
    assert (huge.sheet, huge.scale, huge.preferred) == ("A2", (1, 1000), False)
    with pytest.raises(ValueError, match="do not fit"):
        choose_sheet(_boxy(5e6, 5e6, 5e6))


def test_model_points_map_to_each_view():
    projection = _boxy(1, 1, 1)
    point = (10.0, 20.0, 30.0)
    assert to_view(point, projection.view("plan")) == pytest.approx((10, 20))
    assert to_view(point, projection.view("front")) == pytest.approx((10, 30))
    assert to_view(point, projection.view("right")) == pytest.approx((20, 30))


@pytest.mark.parametrize(
    ("verdict", "text"), [(True, "PASS"), (False, "FAIL"), (None, "NOT CHECKED")]
)
def test_title_block_repeats_the_verdict_and_is_never_evidence(verdict, text):
    layout = layout_sheet(_boxy(600, 400, 300), _sheet(verdict))
    block = layout.title_block
    assert block["ASSURANCE (EXACT GEOMETRY CHECKS)"] == text
    assert block["VERDICT SOURCE"] == "assurance_report.json"
    assert block["STATUS"] == "REVIEW DRAWING - NOT EVIDENCE"
    assert block["DATA"].startswith("SYNTHETIC")
    assert block["SCALE"] == layout.choice.label
    (record,) = layout.rationale
    assert record.rule_id == SHEET_RULE and record.source == "rule"


def test_dimensions_cover_overall_size_nozzles_and_plan_runs():
    sheet = _sheet(
        nozzles=[SheetLabel(id="start", position=(40, 200, 100))],
        routes=[
            SheetRoute(
                line="main",
                points=[
                    (40, 200, 100),
                    (300, 200, 100),
                    (300, 200, 250),
                    (560, 200, 250),
                    (560, 350, 250),
                ],
            )  # fmt: skip
        ],
    )
    layout = layout_sheet(_boxy(600, 400, 300), sheet)
    by_kind = {}
    for dim in layout.dimensions:
        by_kind.setdefault(dim.kind, []).append((dim.label, dim.model_length_mm))
    assert ("plan width", 600) in by_kind["overall"]
    assert ("front height", 300) in by_kind["overall"]
    assert len(by_kind["overall"]) == 6
    assert by_kind["nozzle"] == [("nozzle start x", 40), ("nozzle start y", 200)]
    # the vertical riser is seen end-on in plan and is not dimensioned there
    assert by_kind["run"] == [
        ("line main run 1", 260),
        ("line main run 3", 260),
        ("line main run 4", 150),
    ]
    factor = layout.choice.factor
    for dim in layout.dimensions:
        paper = abs(dim.end[0] - dim.start[0]) + abs(dim.end[1] - dim.start[1])
        assert paper == pytest.approx(dim.model_length_mm * factor, abs=1e-2)


def test_sheet_dxf_audits_clean_with_frame_views_and_dimensions(tmp_path):
    projection = project_shapes([("block", cq.Solid.makeBox(600, 400, 300))])
    layout = layout_sheet(projection, _sheet(nozzles=[SheetLabel(id="n", position=(0, 200, 0))]))
    path = write_sheet_dxf(projection, layout, tmp_path / "sheet.dxf")
    doc = ezdxf.readfile(path)
    auditor = doc.audit()
    assert not auditor.has_errors, [str(e) for e in auditor.errors]
    space = doc.modelspace()
    assert len(space.query("DIMENSION")) == len(layout.dimensions)
    assert len(space.query('LWPOLYLINE[layer=="FRAME"]')) == 2
    texts = " ".join(text.dxf.text for text in space.query("TEXT"))
    assert "ASSURANCE (EXACT GEOMETRY CHECKS): PASS" in texts
    assert "NOT EVIDENCE" in texts
    (x0, y0), (x1, y1) = projection.view("plan").bounds
    ox, oy = layout.view_origins["plan"]
    factor = layout.choice.factor
    assert ox + x0 * factor >= 0 and ox + x1 * factor <= layout.choice.width_mm
    assert oy + y0 * factor >= 0 and oy + y1 * factor <= layout.choice.height_mm
