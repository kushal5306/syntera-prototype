"""The search CLI ranks knowledge, design rationale and drawing graphs together (E2)."""

from __future__ import annotations

from pathlib import Path

import ezdxf

from syntera.cli import main
from syntera.drawings.graph import GRAPH_FILE, build_graph
from syntera.knowledge import load_knowledge
from syntera.knowledge.retrieval import build_index

PROJECT = Path(__file__).parents[2]


def _graph():
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (600, 0), (600, 400), (0, 400)], close=True,
                       dxfattribs={"layer": "EQUIPMENT"})  # fmt: skip
    msp.add_text("FEED PUMP P-101", height=20).set_placement((250, 200))
    msp.add_linear_dim(base=(0, -50), p1=(0, 0), p2=(600, 0)).render()
    return build_graph(doc)


def test_drawing_entities_are_found_by_tag_and_label():
    graph = _graph()
    index = build_index(load_knowledge(), graphs=[("plan", graph)])
    (hit,) = index.search("feed pump P-101", 1)
    assert hit.kind == "drawing" and hit.target == graph.nodes[0].handle
    assert hit.id == f"drawing:plan:{graph.nodes[0].handle}"
    dims = index.search("dimension 600", 3, kinds=["drawing"])
    assert dims[0].title == "linear dimension 600"
    # identical inputs give identical rankings
    again = build_index(load_knowledge(), graphs=[("plan", _graph())])
    assert again.search("feed pump P-101 600") == index.search("feed pump P-101 600")


def test_search_cli_combines_corpora(tmp_path, capsys):
    folder = tmp_path / "plan"
    folder.mkdir()
    (folder / GRAPH_FILE).write_text(_graph().model_dump_json(), encoding="utf-8")
    args = ["search", "--query", "outer diameter P-101 bend radius",
            "--design", str(PROJECT / "examples" / "demo_skid_sized.yaml"),
            "--graph", str(folder / GRAPH_FILE), "--limit", "20"]  # fmt: skip
    assert main(args) == 0
    out = capsys.readouterr().out
    assert "[knowledge] Long-radius bends" in out
    assert "[rationale] lines.main.tube.outer_diameter" in out
    assert "[drawing]" in out and "P-101" in out
