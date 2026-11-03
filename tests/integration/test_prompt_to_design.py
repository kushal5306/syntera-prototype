"""Prompt-to-design: proposals change nothing until the user confirms (F4)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from syntera.assistant.agent import ProviderReply, ScriptedProvider, TextBlock, ToolUseBlock
from syntera.assistant.design_flow import confirm_change, request_change
from syntera.assistant.tools import AssistantContext, ToolError, _proposal_id
from syntera.changes import ChangeSet
from syntera.design import design_bytes, design_from_yaml
from syntera.web.app import create_app

PROJECT = Path(__file__).parents[2]
FIXED = PROJECT / "examples" / "demo_skid.yaml"
WIDER = {"description": "clearance 25 mm for insulation", "edits": [
    {"op": "set_parameter", "path": "lines.main.tube.minimum_clearance", "value": 25,
     "explanation": "insulation allowance"}]}  # fmt: skip


def _use(call_id, name, arguments):
    return ProviderReply(content=[ToolUseBlock(id=call_id, name=name, input=arguments)],
                         stop_reason="tool_use")  # fmt: skip


def _say(text):
    return ProviderReply(content=[TextBlock(text=text)])


def test_an_unconfirmed_proposal_changes_nothing(tmp_path):
    ctx = AssistantContext(design=design_from_yaml(FIXED), output=tmp_path)
    before = design_bytes(ctx.design)
    provider = ScriptedProvider([
        _use("t1", "propose_change", {"changes": WIDER}),
        _say("I propose a 25 mm clearance; please confirm."),
    ])  # fmt: skip
    result = request_change(provider, ctx, "Allow for 5 mm insulation on the pipe")
    (proposal,) = result.proposals
    assert proposal.diff["values"][0]["after"] == 25
    assert design_bytes(ctx.design) == before and result.applied is False
    assert not any(tmp_path.iterdir())  # nothing was routed or written

    rejected = confirm_change(ctx, proposal.proposal_id, confirmed=False)
    assert rejected.applied is False and design_bytes(ctx.design) == before
    with pytest.raises(ToolError, match="unknown proposal"):
        confirm_change(ctx, proposal.proposal_id, confirmed=True)
    assert design_bytes(ctx.design) == before


def test_the_assistant_cannot_apply_its_own_proposal(tmp_path):
    ctx = AssistantContext(design=design_from_yaml(FIXED), output=tmp_path)
    before = design_bytes(ctx.design)
    pid = _proposal_id(ctx.design, ChangeSet.model_validate(WIDER))
    provider = ScriptedProvider([
        _use("t1", "propose_change", {"changes": WIDER}),
        _use("t2", "apply_change", {"proposal_id": pid, "user_confirmed": True}),
        _say("Applied the clearance change."),
    ])  # fmt: skip
    result = request_change(provider, ctx, "Make the clearance 25 mm and apply it")
    assert [p.proposal_id for p in result.proposals] == [pid]
    assert design_bytes(ctx.design) == before
    refusal = [entry for entry in ctx.log if entry["tool"] == "apply_change"][0]
    assert "not confirmed this proposal in the interface" in refusal["error"]


def test_confirmation_applies_regenerates_and_explains(tmp_path):
    ctx = AssistantContext(design=design_from_yaml(FIXED), output=tmp_path)
    provider = ScriptedProvider([_use("t1", "propose_change", {"changes": WIDER}),
                                 _say("Proposed.")])  # fmt: skip
    (proposal,) = request_change(provider, ctx, "insulate the pipe").proposals
    result = confirm_change(ctx, proposal.proposal_id, confirmed=True)
    assert result.applied and ctx.design.line("main").tube.minimum_clearance == 25
    assert result.verdicts["source"].startswith("exact geometry checks")
    assert result.verdicts["overall_pass"] is True
    assert [route.line for route in ctx.design.routes] == ["main"]
    (explained,) = [e for e in result.explanations
                    if e["target"] == "lines.main.tube.minimum_clearance"]  # fmt: skip
    assert explained["source"] == "user" and "insulation allowance" in explained["headline"]
    assert result.revert["edits"]


def test_web_flow_proposes_then_applies_only_on_confirmation(tmp_path):
    provider = ScriptedProvider([_use("t1", "propose_change", {"changes": WIDER}),
                                 _say("Proposed a 25 mm clearance.")])  # fmt: skip
    client = TestClient(create_app(FIXED, tmp_path / "web", assistant_provider=lambda: provider))
    proposed = client.post("/api/assistant/design", json={"request": "insulate it"}).json()
    (proposal,) = proposed["proposals"]
    assert proposed["applied"] is False
    config = client.get("/api/config").json()
    assert config["tube"]["minimum_clearance"] == 20  # the served design is unchanged
    no = client.post("/api/assistant/confirm", json={"proposal_id": proposal["proposal_id"],
                                                     "confirmed": False}).json()  # fmt: skip
    assert no["applied"] is False
    again = client.post("/api/assistant/confirm", json={"proposal_id": proposal["proposal_id"],
                                                        "confirmed": True})  # fmt: skip
    assert again.status_code == 404
