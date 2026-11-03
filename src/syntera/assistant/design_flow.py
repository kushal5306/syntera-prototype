"""Prompt-to-design with confirmation (roadmap F4).

1. ``request_change``: the assistant turns a request into proposed change sets (through
   ``propose_change``). Nothing is applied; each proposal comes back with its diff.
2. ``confirm_change``: only the user's decision applies a proposal. On confirmation the
   change is applied, the deterministic pipeline routes and verifies the design, and the
   changed values are explained from their rationale. On rejection the proposal is
   discarded. Either way the assistant never applies anything by itself.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict

from syntera.assistant.agent import Provider, ToolResultBlock, ToolUseBlock, run_agent
from syntera.assistant.tools import AssistantContext, ToolError, call_tool
from syntera.design import design_sha256


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PendingProposal(StrictModel):
    proposal_id: str
    description: str
    diff: dict[str, Any]


class DesignRequestResult(StrictModel):
    answer: str
    proposals: list[PendingProposal]
    design_sha256: str
    applied: bool = False


class ConfirmationResult(StrictModel):
    proposal_id: str
    applied: bool
    design_sha256: str
    verdicts: dict[str, Any] | None = None
    explanations: list[dict[str, Any]] = []
    revert: dict[str, Any] | None = None
    message: str


def request_change(provider: Provider, ctx: AssistantContext, request: str) -> DesignRequestResult:
    """Let the assistant propose changes for ``request``; returns proposals, applies none."""
    result = run_agent(provider, ctx, request)
    uses: dict[str, ToolUseBlock] = {}
    proposals: list[PendingProposal] = []
    for message in result.transcript:
        for block in message.content:
            if isinstance(block, ToolUseBlock):
                uses[block.id] = block
            elif isinstance(block, ToolResultBlock) and not block.is_error:
                use = uses.get(block.tool_use_id)
                if use is None or use.name != "propose_change":
                    continue
                data = json.loads(block.content)
                if data["proposal_id"] in ctx.proposals:
                    proposals.append(
                        PendingProposal(
                            proposal_id=data["proposal_id"],
                            description=use.input["changes"]["description"],
                            diff=data["diff"],
                        )
                    )
    return DesignRequestResult(
        answer=result.answer, proposals=proposals, design_sha256=design_sha256(ctx.design)
    )


def confirm_change(ctx: AssistantContext, proposal_id: str, confirmed: bool) -> ConfirmationResult:
    """Apply (and regenerate and explain) or discard a proposal, as the user decided."""
    if proposal_id not in ctx.proposals:
        raise ToolError(f"unknown proposal {proposal_id}")
    if not confirmed:
        del ctx.proposals[proposal_id]
        return ConfirmationResult(
            proposal_id=proposal_id,
            applied=False,
            design_sha256=design_sha256(ctx.design),
            message="Discarded at the user's request; the design is unchanged.",
        )
    ctx.confirm(proposal_id)
    applied = call_tool(ctx, "apply_change", {"proposal_id": proposal_id, "user_confirmed": True})
    verdicts = call_tool(ctx, "regenerate", {})
    previous = ctx.history[-1]
    from syntera.changes import diff_designs

    explanations = []
    for change in diff_designs(previous, ctx.design).rationale:
        if change.after is None:
            continue
        try:
            explanations.append(call_tool(ctx, "explain", {"path": change.path}))
        except ToolError:
            continue
    outcome = "passes" if verdicts["overall_pass"] else "does not pass"
    return ConfirmationResult(
        proposal_id=proposal_id,
        applied=True,
        design_sha256=design_sha256(ctx.design),
        verdicts=verdicts,
        explanations=explanations,
        revert=applied["revert"],
        message=f"Applied and regenerated: the exact checks report the design {outcome}.",
    )
