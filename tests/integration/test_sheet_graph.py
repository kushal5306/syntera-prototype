"""Syntera's own sheets read back into a drawing graph keyed by design references (D3)."""

from __future__ import annotations

from pathlib import Path

from syntera.cli import main
from syntera.drawings.graph import dimensions_of, find_by_tag, graph_from_file
from syntera.drawings.sheet import SHEET_DXF

PROJECT = Path(__file__).parents[2]


def test_sheet_graph_finds_design_items_and_their_dimensions(tmp_path, capsys):
    run = tmp_path / "demo"
    assert main(["demo", "--config", str(PROJECT / "examples" / "demo_skid.yaml"),
                 "--output", str(run)]) == 0  # fmt: skip
    assert main(["sheet", "--run", str(run), "--output", str(tmp_path / "sheet")]) == 0
    path = tmp_path / "sheet" / SHEET_DXF
    graph = graph_from_file(path)

    main_line = find_by_tag(graph, "main")
    assert main_line and all(node.ref == "line:main" and node.kind == "line" for node in main_line)
    runs = {d.handle for node in main_line for d in dimensions_of(graph, node.handle)}
    assert runs and all(d.ref == "line:main" for d in graph.dimensions if d.handle in runs)
    vessel = find_by_tag(graph, "central-process-vessel")
    assert vessel and all(node.kind == "equipment" for node in vessel)
    assert {node.ref for node in find_by_tag(graph, "start")} == {"nozzle:start"}
    assert graph == graph_from_file(path)

    out = tmp_path / "graph"
    args = ["drawing-graph", "--input", str(path), "--output", str(out), "--tag", "main"]
    assert main(args) == 0
    assert "Tag main:" in capsys.readouterr().out
