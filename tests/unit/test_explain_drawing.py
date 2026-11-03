"""Explanations of foreign drawings are always labelled as inference (E3)."""

from __future__ import annotations

import math

import ezdxf
import pytest

from syntera.cli import main
from syntera.drawings.graph import GRAPH_FILE, build_graph
from syntera.knowledge.explain import (
    INFERENCE_NOTE,
    ExplainError,
    back_calculate_velocity,
    explain_entity,
    stated_diameter,
)


def _graph():
    doc = ezdxf.new("R2010", setup=True)
    msp = doc.modelspace()
    pipe = msp.add_line((0, 0), (1000, 0), dxfattribs={"layer": "PIPE"})
    msp.add_text("L-001 OD 60.3", height=10).set_placement((400, 15))
    nominal = msp.add_line((0, 500), (1000, 500), dxfattribs={"layer": "PIPE"})
    msp.add_text("L-002 DN50", height=10).set_placement((400, 515))
    bare = msp.add_line((0, 900), (1000, 900), dxfattribs={"layer": "PIPE"})
    nozzle = msp.add_circle((2000, 0), 30, dxfattribs={"layer": "NOZZLE"})
    msp.add_diameter_dim(center=(2000, 0), radius=30, angle=45).render()
    graph = build_graph(doc)
    return graph, {
        "pipe": pipe.dxf.handle, "nominal": nominal.dxf.handle, "bare": bare.dxf.handle,
        "nozzle": nozzle.dxf.handle,
    }  # fmt: skip


def test_stated_diameters_come_from_dimensions_and_labels():
    graph, h = _graph()
    assert stated_diameter(graph, h["pipe"]) == (60.3, "label 'L-001 OD 60.3'", False)
    value, found, nominal = stated_diameter(graph, h["nominal"])
    assert (value, nominal) == (60.3, True) and "DN50" in found
    value, found, nominal = stated_diameter(graph, h["nozzle"])
    assert value == pytest.approx(60) and found.startswith("diameter dimension")
    with pytest.raises(ExplainError, match="no diameter is stated"):
        stated_diameter(graph, h["bare"])


def test_velocity_back_calculation_is_hand_checked_and_labelled_inferred():
    graph, h = _graph()
    result = back_calculate_velocity(graph, h["pipe"], "plan", 36.0, wall_mm=3.9)
    expected = 36.0 / 3600 / (math.pi * 0.0525**2 / 4)
    assert result.value == pytest.approx(expected, abs=1e-4)
    assert result.inferred and result.source == "inferred"
    assert result.headline.startswith("Inferred: about ")
    assert result.caveats[0] == INFERENCE_NOTE
    assert "Bore = 60.3 - 2 x 3.9 = 52.5 mm." in result.steps
    assert result.rule_id == "rules.flow_velocity_from_flow_and_bore.v1"
    no_wall = back_calculate_velocity(graph, h["nominal"], "plan", 36.0, mode="engineer")
    assert any("outside diameter is used as the bore" in c for c in no_wall.caveats)
    assert any("nominal (DN)" in c for c in no_wall.caveats)


def test_every_drawing_explanation_is_inference():
    graph, h = _graph()
    for handle in h.values():
        for mode in ("plain", "engineer"):
            explanation = explain_entity(graph, handle, "plan", mode)
            assert explanation.inferred and explanation.source == "inferred"
            assert explanation.headline.startswith("Inferred:")
            assert INFERENCE_NOTE in explanation.caveats
    pipe = explain_entity(graph, h["pipe"], "plan")
    assert "'L-001 OD 60.3'" in pipe.headline
    with pytest.raises(ExplainError, match="no drawing entity"):
        explain_entity(graph, "FFFF", "plan")


def test_explain_cli_for_design_and_drawing(tmp_path, capsys):
    from pathlib import Path

    sized = Path(__file__).parents[2] / "examples" / "demo_skid_sized.yaml"
    assert main(["explain", "--design", str(sized), "--path", "tube.outer_diameter"]) == 0
    assert capsys.readouterr().out.startswith("Set by a rule: OD 20 mm")
    graph, h = _graph()
    path = tmp_path / "plan" / GRAPH_FILE
    path.parent.mkdir()
    path.write_text(graph.model_dump_json(), encoding="utf-8")
    args = ["explain", "--graph", str(path), "--handle", h["pipe"], "--flow", "36", "--wall", "3.9"]
    assert main(args) == 0
    out = capsys.readouterr().out
    assert out.startswith("Inferred: about 4.6") and "Note: Inferred from the drawing" in out
