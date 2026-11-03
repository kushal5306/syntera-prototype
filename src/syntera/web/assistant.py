"""``/api/assistant``: answer questions about the design or a stored drawing (roadmap F3).

The endpoint runs the agent loop over the assistant tools with the configured provider
(``ANTHROPIC_API_KEY``) or, in tests, a scripted one. Answers come with citations taken
from the tool results (rationale paths, rule ids, knowledge entries, drawing handles),
the tools that ran, and whether any check ran. The browser only receives this display
data; a verdict can only come from the checks the tools ran.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from threading import Lock
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from syntera.assistant.agent import (
    TRANSCRIPT_FILE,
    AgentResult,
    Message,
    Provider,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    provider_from_env,
    run_agent,
)
from syntera.assistant.tools import AssistantContext
from syntera.design import load_design_input

DRAWING_ID = re.compile(r"^[0-9a-f]{16}$")
GRAPH_FILE = "drawing_graph.json"
ProviderFactory = Callable[[], Provider | None]


class Turn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    text: str = Field(max_length=8000)


class AssistantQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)
    drawing_id: str | None = None
    history: list[Turn] = Field(default_factory=list, max_length=20)


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["rationale", "rule", "knowledge", "drawing", "proposal", "check"]
    id: str


def citations(result: AgentResult) -> list[Citation]:
    """What the answer rests on, read from the tool calls and their results."""
    found: dict[tuple[str, str], None] = {}
    uses: dict[str, ToolUseBlock] = {}
    for message in result.transcript:
        for block in message.content:
            if isinstance(block, ToolUseBlock):
                uses[block.id] = block
            if not isinstance(block, ToolResultBlock) or block.is_error:
                continue
            use = uses.get(block.tool_use_id)
            if use is None:
                continue
            data = json.loads(block.content)
            for kind, value in _cited(use.name, use.input, data):
                found[(kind, value)] = None
    return [Citation(kind=kind, id=value) for kind, value in found]  # type: ignore[arg-type]


def _cited(name: str, arguments: dict[str, Any], data: dict[str, Any]):
    if name == "explain":
        if data.get("inferred"):
            yield "drawing", data["target"]
        else:
            yield "rationale", data["target"]
        if data.get("rule_id"):
            yield "rule", data["rule_id"]
        for entry in data.get("knowledge", []):
            yield "knowledge", entry
    elif name == "query_design" and "path" in data:
        yield "rationale", data["path"]
    elif name == "query_drawing":
        drawing = arguments.get("drawing", "")
        for entity in data.get("entities", []) if isinstance(data.get("entities"), list) else []:
            yield "drawing", f"drawing:{drawing}:{entity['handle']}"
        if "handle" in data:
            yield "drawing", f"drawing:{drawing}:{data['handle']}"
    elif name == "search":
        for hit in data.get("hits", []):
            yield hit["kind"], hit["id"] if hit["kind"] == "drawing" else hit["target"]
    elif name == "propose_change":
        yield "proposal", data["proposal_id"]
    elif name in {"run_checks", "regenerate"}:
        yield "check", data.get("output", name)


def assistant_router(
    config_path: Path, output_root: Path, provider_factory: ProviderFactory = provider_from_env
) -> APIRouter:
    router = APIRouter(prefix="/api/assistant")
    lock = Lock()
    state: dict[str, AssistantContext] = {}

    def context() -> AssistantContext:
        if "ctx" not in state:
            state["ctx"] = AssistantContext(
                design=load_design_input(config_path), output=output_root / "assistant"
            )
        return state["ctx"]

    @router.get("/status")
    def status() -> dict:
        provider = provider_factory()
        return {"available": provider is not None,
                "provider": provider.name if provider else None}  # fmt: skip

    @router.post("")
    def ask(question: AssistantQuestion) -> dict:
        provider = provider_factory()
        if provider is None:
            raise HTTPException(
                status_code=503,
                detail="No language model is configured: set ANTHROPIC_API_KEY on the server.",
            )
        with lock:
            ctx = context()
            if question.drawing_id is not None:
                if not DRAWING_ID.match(question.drawing_id):
                    raise HTTPException(status_code=422, detail="invalid drawing id")
                graph_path = output_root / "drawings" / question.drawing_id / GRAPH_FILE
                if not graph_path.is_file():
                    raise HTTPException(status_code=404, detail="drawing not found or not read")
                from syntera.drawings.graph import DrawingGraph

                ctx.graphs[question.drawing_id] = DrawingGraph.model_validate_json(
                    graph_path.read_bytes()
                )
            history = [
                Message(role=turn.role, content=[TextBlock(text=turn.text)])
                for turn in question.history
            ]
            result = run_agent(provider, ctx, question.question, history=history,
                               transcript_path=ctx.output / TRANSCRIPT_FILE)  # fmt: skip
        return {
            "answer": result.answer,
            "stopped": result.stopped,
            "steps": result.steps,
            "tool_calls": result.tool_calls,
            "checks_run": result.checks_run,
            "citations": [c.model_dump(mode="json") for c in citations(result)],
            "design_title": ctx.design.title,
        }

    return router
