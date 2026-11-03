"""Typed assistant tools over deterministic functions, and the assurance guard (F1)."""

from __future__ import annotations

import json
from pathlib import Path

import ezdxf
import pytest

from syntera.assistant.tools import (
    TOOLS,
    VERDICT_WORDS,
    AssistantContext,
    ToolError,
    call_tool,
    schema_field_names,
    tool_schemas,
)
from syntera.design import design_from_yaml
from syntera.drawings.graph import build_graph

PROJECT = Path(__file__).parents[2]
FIXED = PROJECT / "examples" / "demo_skid.yaml"
SIZED = PROJECT / "examples" / "demo_skid_sized.yaml"
CLEARANCE = {
    "description": "wider clearance",
    "edits": [{"op": "set_parameter", "path": "lines.main.tube.minimum_clearance",
               "value": 25}],
}  # fmt: skip


def _graph():
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (600, 0), (600, 400), (0, 400)], close=True,
                       dxfattribs={"layer": "EQUIPMENT"})  # fmt: skip
    msp.add_text("PUMP P-101", height=20).set_placement((250, 200))
    return build_graph(doc)


@pytest.fixture
def ctx(tmp_path):
    return AssistantContext(design=design_from_yaml(SIZED), output=tmp_path,
                            graphs={"plan": _graph()})  # fmt: skip


def test_every_tool_exports_a_json_schema():
    schemas = tool_schemas()
    names = [schema["name"] for schema in schemas]
    assert names == [tool.name for tool in TOOLS] and len(set(names)) == len(names)
    assert set(names) == {"query_design", "query_drawing", "explain", "search", "run_checks",
                          "propose_change", "apply_change", "regenerate", "compare"}  # fmt: skip
    for schema in schemas:
        assert schema["input_schema"]["type"] == "object"
        assert schema["description"]
        json.dumps(schema)


def test_guard_no_tool_input_can_carry_a_verdict(ctx):
    for schema in tool_schemas():
        assert not schema_field_names(schema["input_schema"]) & VERDICT_WORDS, schema["name"]
    for name in ("run_checks", "regenerate", "compare"):
        with pytest.raises(ToolError, match="invalid arguments"):
            call_tool(ctx, name, {"overall_pass": True})
    forbidden = [
        {"op": "set_routes", "routes": []},
        {"op": "set_rationale", "path": "lines.main.tube.outer_diameter", "record": None},
        {"op": "set_parameter", "path": "lines.main.analysis.acceptance.stress_safety_factor",
         "value": 0.5},
        {"op": "set_parameter", "path": "lines.main.analysis.acceptance.maximum_displacement_mm",
         "value": 100},
    ]  # fmt: skip
    for edit in forbidden:
        with pytest.raises(ToolError, match="not available to the assistant|only be set by"):
            call_tool(ctx, "propose_change", {"changes": {"description": "x", "edits": [edit]}})
    assert ctx.proposals == {}
    assert all("error" in entry for entry in ctx.log)


def test_query_design_and_drawing(ctx):
    summary = call_tool(ctx, "query_design", {"what": "summary"})
    assert summary["lines"][0]["outer_diameter_mm"] == 20 and summary["routed"] == []
    value = call_tool(ctx, "query_design", {"what": "value",
                                            "path": "lines.main.tube.outer_diameter"})  # fmt: skip
    assert value["source"] == "rule" and value["text"].startswith("Set by a rule")
    assert call_tool(ctx, "query_design", {"what": "line", "id": "main"})["id"] == "main"
    with pytest.raises(ToolError, match="unknown line"):
        call_tool(ctx, "query_design", {"what": "line", "id": "x"})
    found = call_tool(ctx, "query_drawing", {"drawing": "plan", "tag": "P-101"})
    assert found["entities"][0]["labels"] == ["PUMP P-101"]
    with pytest.raises(ToolError, match="no drawing elevation"):
        call_tool(ctx, "query_drawing", {"drawing": "elevation", "tag": "P-101"})


def test_explain_and_search(ctx):
    why = call_tool(ctx, "explain", {"path": "tube.minimum_bend_radius"})
    assert why["rule_id"] == "rules.bend_radius_long_radius.v1" and not why["inferred"]
    handle = ctx.graphs["plan"].nodes[0].handle
    inferred = call_tool(ctx, "explain", {"drawing": "plan", "handle": handle})
    assert inferred["inferred"] is True
    hits = call_tool(ctx, "search", {"query": "bend radius long radius", "limit": 3})["hits"]
    assert hits[0]["kind"] in {"knowledge", "rationale"}
    with pytest.raises(ToolError, match="give a path"):
        call_tool(ctx, "explain", {})


def test_proposals_change_nothing_until_the_user_confirms(ctx):
    before = ctx.design
    proposal = call_tool(ctx, "propose_change", {"changes": CLEARANCE})
    assert proposal["applied"] is False
    assert ctx.design is before
    assert proposal["diff"]["values"][0]["path"] == "lines.main.tube.minimum_clearance"
    pid = proposal["proposal_id"]
    with pytest.raises(ToolError, match="not confirmed"):
        call_tool(ctx, "apply_change", {"proposal_id": pid, "user_confirmed": False})
    assert ctx.design is before
    # the assistant claiming confirmation is not enough: the user must confirm
    with pytest.raises(ToolError, match="not confirmed this proposal in the interface"):
        call_tool(ctx, "apply_change", {"proposal_id": pid, "user_confirmed": True})
    assert ctx.design is before
    ctx.confirm(pid)
    applied = call_tool(ctx, "apply_change", {"proposal_id": pid, "user_confirmed": True})
    assert applied["applied"] is True
    assert ctx.design.line("main").tube.minimum_clearance == 25
    assert ctx.history == [before]
    with pytest.raises(ToolError, match="unknown proposal"):
        call_tool(ctx, "apply_change", {"proposal_id": pid, "user_confirmed": True})
    diff = call_tool(ctx, "compare", {})
    assert [v["path"] for v in diff["values"]] == ["lines.main.tube.minimum_clearance"]


def test_stale_proposals_are_refused(ctx):
    first = call_tool(ctx, "propose_change", {"changes": CLEARANCE})["proposal_id"]
    other = {"description": "grid", "edits": [
        {"op": "set_parameter", "path": "parameters.voxel_resolution", "value": 10}]}  # fmt: skip
    second = call_tool(ctx, "propose_change", {"changes": other})["proposal_id"]
    ctx.confirm(second)
    call_tool(ctx, "apply_change", {"proposal_id": second, "user_confirmed": True})
    ctx.confirm(first)
    with pytest.raises(ToolError, match="design changed since"):
        call_tool(ctx, "apply_change", {"proposal_id": first, "user_confirmed": True})


def test_verdicts_come_only_from_the_checks(tmp_path):
    ctx = AssistantContext(design=design_from_yaml(FIXED), output=tmp_path)
    checks = call_tool(ctx, "run_checks", {})
    assert checks["overall_pass"] is True and checks["lines"] == {"main": True}
    assert checks["source"].startswith("exact geometry checks")
    assert ctx.design.routes == []  # run_checks does not change the design
    regenerated = call_tool(ctx, "regenerate", {})
    assert regenerated["overall_pass"] is True
    assert [route.line for route in ctx.design.routes] == ["main"]


def test_cli_prints_the_schemas(capsys):
    from syntera.cli import main

    assert main(["assistant-tools"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert [tool["name"] for tool in printed] == [tool.name for tool in TOOLS]
