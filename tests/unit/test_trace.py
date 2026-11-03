"""Drawing-to-model references: tagging, resolving and tracing DXF entities (C4)."""

from __future__ import annotations

from pathlib import Path

import ezdxf

from syntera.design import design_from_yaml, parse_design
from syntera.drawings.trace import APPID, design_shapes, ref_of, resolve, tag, trace_dxf

PROJECT = Path(__file__).parents[2]
DESIGN = design_from_yaml(PROJECT / "examples" / "demo_skid.yaml")


def test_references_resolve_to_design_items_views_and_sheet_parts():
    assert resolve("equipment:central-process-vessel", DESIGN).kind == "equipment"
    assert resolve("nozzle:start", DESIGN).id == "start"
    assert resolve("line:main", DESIGN).kind == "line"
    assert resolve("view:plan", DESIGN).kind == "view"
    assert resolve("drawing:title-block", DESIGN).kind == "drawing"
    for missing in ("line:ghost", "equipment:", "view:section", "fitting:elbow-1", "main"):
        assert resolve(missing, DESIGN) is None, missing


def test_tags_round_trip_through_a_saved_dxf(tmp_path):
    doc = ezdxf.new("R2018")
    doc.appids.new(APPID)
    space = doc.modelspace()
    tag(space.add_line((0, 0), (1, 0)), "line:main")
    tag(space.add_line((0, 1), (1, 1)), "line:ghost")
    space.add_line((0, 2), (1, 2))
    path = tmp_path / "t.dxf"
    doc.saveas(path)
    entities = list(ezdxf.readfile(path).modelspace())
    assert [ref_of(entity) for entity in entities] == ["line:main", "line:ghost", None]

    report = trace_dxf(path, DESIGN)
    assert (report.entities, report.resolved) == (3, 1)
    assert report.by_kind == {"line": 1}
    assert len(report.untagged) == 1 and report.untagged[0].startswith("LINE")
    assert report.unresolved[0].endswith("line:ghost")


def test_design_shapes_label_equipment_nozzles_and_routed_lines_only():
    labels = [label for label, _ in design_shapes(DESIGN)]
    assert labels == [
        "equipment:central-process-vessel", "equipment:auxiliary-pump",
        "nozzle:start", "nozzle:end",
    ]  # fmt: skip
    data = DESIGN.model_dump(mode="json")
    data["routes"] = [{"line": "main", "points": [[40, 200, 100], [560, 200, 100]],
                       "length_mm": 520}]  # fmt: skip
    assert "line:main" in [label for label, _ in design_shapes(parse_design(data))]
