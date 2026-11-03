"""Semantic drawing graph: label association, dimension links, title block, queries (D3)."""

from __future__ import annotations

import json
from pathlib import Path

import ezdxf

from syntera.drawings.graph import (
    GRAPH_FILE,
    dimensions_of,
    find_by_tag,
    graph_from_file,
    label_of,
)
from syntera.drawings.intake import ingest_drawing


def plant_dxf(path: Path) -> Path:
    doc = ezdxf.new("R2010", setup=True)
    doc.units = ezdxf.units.MM
    block = doc.blocks.new("TB")
    for index, tag in enumerate(["TITLE", "DWG_NO", "REV"]):
        block.add_attdef(tag, (0, 5 * index), dxfattribs={"height": 2.5})
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (600, 0), (600, 400), (0, 400)], close=True,
                       dxfattribs={"layer": "EQUIPMENT"})  # fmt: skip
    msp.add_circle((300, 200), 40, dxfattribs={"layer": "NOZZLES"})
    msp.add_line((600, 200), (900, 200), dxfattribs={"layer": "PIPE"})
    msp.add_text("PUMP P-101", height=20).set_placement((250, 200))
    msp.add_text("L-001", height=5).set_placement((700, 210))
    msp.add_text("NOTES", height=5).set_placement((5000, 5000))
    msp.add_linear_dim(base=(0, -50), p1=(0, 0), p2=(600, 0)).render()
    msp.add_linear_dim(base=(600, 150), p1=(600, 200), p2=(900, 200)).render()
    msp.add_blockref("TB", (1000, 0)).add_auto_attribs(
        {"TITLE": "Pump skid", "DWG_NO": "SYN-002", "REV": "A"}
    )
    doc.saveas(path)
    return path


def _handles(doc_path: Path) -> dict[str, str]:
    space = ezdxf.readfile(doc_path).modelspace()
    return {
        "rect": space.query("LWPOLYLINE")[0].dxf.handle,
        "circle": space.query("CIRCLE")[0].dxf.handle,
        "pipe": space.query("LINE")[0].dxf.handle,
    }


def test_labels_attach_to_the_nearest_or_enclosing_geometry(tmp_path):
    path = plant_dxf(tmp_path / "p.dxf")
    handles = _handles(path)
    graph = graph_from_file(path)
    attached = {label.text: label.attached_to for label in graph.labels}
    assert attached == {"PUMP P-101": handles["rect"], "L-001": handles["pipe"], "NOTES": None}
    assert graph.node(handles["rect"]).kind == "equipment"
    assert graph.node(handles["pipe"]).kind == "pipe"
    assert graph.node(handles["circle"]).kind == "geometry"


def test_dimensions_link_to_the_geometry_they_measure(tmp_path):
    path = plant_dxf(tmp_path / "p.dxf")
    handles = _handles(path)
    graph = graph_from_file(path)
    width, pipe_run = graph.dimensions
    assert width.measurement == 600 and width.measures == [handles["rect"]]
    assert pipe_run.measurement == 300 and handles["pipe"] in pipe_run.measures
    assert handles["circle"] not in width.measures + pipe_run.measures


def test_title_block_facts_and_queries(tmp_path):
    path = plant_dxf(tmp_path / "p.dxf")
    handles = _handles(path)
    graph = graph_from_file(path)
    assert graph.title_block == {"DWG_NO": "SYN-002", "REV": "A", "TITLE": "Pump skid"}
    (pump,) = find_by_tag(graph, "p-101")
    assert pump.handle == handles["rect"]
    assert label_of(graph, pump.handle) == ["PUMP P-101"]
    assert [d.measurement for d in dimensions_of(graph, pump.handle)] == [600, 300]
    assert [n.handle for n in find_by_tag(graph, "L-001")] == [handles["pipe"]]
    assert find_by_tag(graph, "X-999") == []


def test_graph_is_deterministic_and_written_on_ingest(tmp_path):
    path = plant_dxf(tmp_path / "p.dxf")
    assert graph_from_file(path) == graph_from_file(path)
    summary = ingest_drawing(path.read_bytes(), "p.dxf", tmp_path / "store")
    stored = json.loads((tmp_path / "store" / summary.drawing_id / GRAPH_FILE).read_text())
    assert stored["title_block"]["DWG_NO"] == "SYN-002"
    assert stored == json.loads(graph_from_file(path).model_dump_json())
