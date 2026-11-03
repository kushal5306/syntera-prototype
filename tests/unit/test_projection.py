"""Exact hidden-line projection into plan, front, right and isometric views (C1)."""

from __future__ import annotations

import cadquery as cq
import ezdxf
import pytest

from syntera.drawings.projection import (
    LAYER_HIDDEN,
    LAYER_VISIBLE,
    STANDARD_VIEWS,
    project_shapes,
    view_offsets,
    write_projection_dxf,
)


def _box(size, at=(0, 0, 0)) -> cq.Shape:
    return cq.Solid.makeBox(*size).translate(cq.Vector(*at))


def _approx_bounds(view, expected):
    (x0, y0), (x1, y1) = view.bounds
    (ex0, ey0), (ex1, ey1) = expected
    assert (x0, y0, x1, y1) == pytest.approx((ex0, ey0, ex1, ey1), abs=1e-3)


def test_orthographic_views_map_model_axes_to_the_sheet():
    projection = project_shapes([("block", _box((100, 60, 40)))])
    assert [view.name for view in projection.views] == [spec.name for spec in STANDARD_VIEWS]
    _approx_bounds(projection.view("plan"), ((0, 0), (100, 60)))
    _approx_bounds(projection.view("front"), ((0, 0), (100, 40)))
    _approx_bounds(projection.view("right"), ((0, 0), (60, 40)))
    plan = projection.view("plan")
    assert plan.count(True) >= 4
    assert all(polyline.source == "block" for polyline in plan.polylines)
    iso = projection.view("isometric")
    assert iso.count(True) >= 9  # three faces of a box are visible from the corner


def test_a_solid_behind_another_is_hidden_in_front_but_visible_in_plan():
    plate = _box((100, 10, 100))
    behind = _box((40, 40, 40), at=(30, 50, 30))
    projection = project_shapes([("plate", plate), ("behind", behind)])

    def edges(view_name, source, visible):
        view = projection.view(view_name)
        return [p for p in view.polylines if p.source == source and p.visible is visible]

    assert edges("front", "behind", True) == []
    assert len(edges("front", "behind", False)) >= 4
    assert len(edges("plan", "behind", True)) >= 4
    assert len(edges("front", "plate", True)) >= 4


def test_projection_is_deterministic_and_canonical():
    shapes = [
        ("plate", _box((100, 10, 100))),
        ("pipe", cq.Solid.makeCylinder(15, 200, cq.Vector(-50, 60, 50), cq.Vector(1, 0, 0))),
    ]
    first = project_shapes(shapes)
    second = project_shapes(shapes)
    assert first.model_dump()["views"] == second.model_dump()["views"]
    for view in first.views:
        keys = [(p.source, not p.visible, p.edge, p.points) for p in view.polylines]
        assert keys == sorted(keys)
        assert all(p.points <= p.points[::-1] for p in view.polylines)
    # a cylinder seen side-on has silhouette (outline) edges, not just sharp ones
    assert any(p.edge == "outline" and p.source == "pipe" for p in first.view("plan").polylines)


def test_labels_must_be_unique_and_present():
    with pytest.raises(ValueError, match="unique"):
        project_shapes([("a", _box((1, 1, 1))), ("a", _box((1, 1, 1)))])
    with pytest.raises(ValueError, match="nothing to project"):
        project_shapes([])


def test_views_dxf_separates_visible_and_hidden_layers_and_audits_clean(tmp_path):
    projection = project_shapes(
        [("plate", _box((100, 10, 100))), ("behind", _box((40, 40, 40), at=(30, 50, 30)))]
    )
    path = write_projection_dxf(projection, tmp_path / "views.dxf")
    doc = ezdxf.readfile(path)
    assert not doc.audit().has_errors
    assert doc.layers.get(LAYER_HIDDEN).dxf.linetype == "DASHED"
    space = doc.modelspace()
    visible = sum(view.count(True) for view in projection.views)
    hidden = sum(view.count(False) for view in projection.views)
    assert len(space.query(f'LWPOLYLINE[layer=="{LAYER_VISIBLE}"]')) == visible
    assert len(space.query(f'LWPOLYLINE[layer=="{LAYER_HIDDEN}"]')) == hidden
    assert hidden > 0
    labels = {text.dxf.text for text in space.query("TEXT")}
    assert labels == {"PLAN", "FRONT", "RIGHT", "ISOMETRIC"}


def test_views_are_placed_without_overlapping():
    projection = project_shapes([("block", _box((100, 60, 40)))])
    offsets = view_offsets(projection)
    boxes = []
    for view in projection.views:
        (x0, y0), (x1, y1) = view.bounds
        dx, dy = offsets[view.name]
        boxes.append((x0 + dx, y0 + dy, x1 + dx, y1 + dy))
    for i, a in enumerate(boxes):
        for b in boxes[i + 1 :]:
            assert a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1]
