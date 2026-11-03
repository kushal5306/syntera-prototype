"""Typed assistant tools with JSON schemas (roadmap F1).

Each tool has a pydantic input model, a deterministic implementation and a JSON schema
export in the shape language-model tool APIs expect. Verdicts only ever come out of the
``run_checks`` tool, which runs the exact geometry checks; no tool accepts a verdict as
input, and change sets can only edit design inputs.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from syntera.changes import ChangeSet, apply_change_set, diff_designs, verdict_changes
from syntera.design import DesignDocument, DesignError
from syntera.knowledge.store import KnowledgeBase, load_knowledge

# Words that name a verdict; no tool input may contain a field with one of these names.
VERDICT_WORDS = frozenset(
    {"overall_pass", "passed", "pass", "verdict", "assurance", "approved", "collision_free"}
)


class ToolError(ValueError):
    """A tool call that cannot be carried out; the message is shown to the assistant."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass
class Proposal:
    id: str
    changes: ChangeSet
    design_sha256: str


@dataclass
class AssistantContext:
    """What the tools act on: the current design, read drawings, and run outputs."""

    design: DesignDocument
    output: Path
    graphs: dict[str, Any] = field(default_factory=dict)
    kb: KnowledgeBase = field(default_factory=load_knowledge)
    proposals: dict[str, Proposal] = field(default_factory=dict)
    history: list[DesignDocument] = field(default_factory=list)
    reports: list[Any] = field(default_factory=list)
    log: list[dict[str, Any]] = field(default_factory=list)


# ---- inputs ------------------------------------------------------------------------------


class QueryDesign(StrictModel):
    """Read part of the current design document."""

    what: Literal["summary", "line", "equipment", "nozzle", "value"]
    id: str | None = Field(default=None, description="line, equipment or nozzle id")
    path: str | None = Field(default=None, description="rationale path for 'value'")


class QueryDrawing(StrictModel):
    """Find entities of a read drawing by tag, or describe one entity by handle."""

    drawing: str
    tag: str | None = None
    handle: str | None = None


class Explain(StrictModel):
    """Explain a design value, or (as inference) a drawing entity or its flow velocity."""

    path: str | None = Field(default=None, description="rationale path in the design")
    drawing: str | None = None
    handle: str | None = None
    flow_m3_per_h: float | None = Field(default=None, gt=0)
    wall_mm: float | None = Field(default=None, gt=0)
    mode: Literal["plain", "engineer"] = "plain"


class Search(StrictModel):
    """Keyword search over knowledge, design rationale and read drawings."""

    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=5, ge=1, le=20)


class RunChecks(StrictModel):
    """Route every line and run the exact geometry checks on the current design."""


class ProposeChange(StrictModel):
    """Validate a change set against the current design and show its diff, without applying it."""

    changes: ChangeSet


class ApplyChange(StrictModel):
    """Apply a proposal the user has confirmed."""

    proposal_id: str = Field(pattern=r"^[0-9a-f]{12}$")
    user_confirmed: bool = Field(description="true only after the user explicitly agreed")


class Regenerate(StrictModel):
    """Route and verify the current design and store the routes in it."""


class Compare(StrictModel):
    """Compare the current design with the previous version (values, rationale, verdicts)."""


# ---- implementations ---------------------------------------------------------------------


def _query_design(ctx: AssistantContext, call: QueryDesign) -> dict[str, Any]:
    design = ctx.design
    try:
        if call.what == "summary":
            return {
                "title": design.title,
                "synthetic_data": design.synthetic_data,
                "equipment": [item.id for item in design.equipment],
                "lines": [
                    {
                        "id": line.id,
                        "from": line.from_nozzle,
                        "to": line.to_nozzle,
                        "outer_diameter_mm": line.tube.outer_diameter,
                    }  # fmt: skip
                    for line in design.lines
                ],
                "routed": sorted(route.line for route in design.routes),
            }
        if call.what == "line":
            return design.line(call.id).model_dump(mode="json")
        if call.what == "nozzle":
            return design.nozzle(call.id or "").model_dump(mode="json")
        if call.what == "equipment":
            for item in design.equipment:
                if item.id == call.id:
                    return item.model_dump(mode="json")
            raise ToolError(f"unknown equipment {call.id}")
        records = {record.path: record for record in design.rationale}
        if call.path not in records:
            raise ToolError(f"no value at {call.path}")
        record = records[call.path]
        return {"path": record.path, "value": record.value, "unit": record.unit,
                "source": record.source, "text": record.text()}  # fmt: skip
    except DesignError as error:
        raise ToolError(str(error)) from error


def _graph(ctx: AssistantContext, name: str):
    if name not in ctx.graphs:
        raise ToolError(f"no drawing {name}; read drawings: {sorted(ctx.graphs)}")
    return ctx.graphs[name]


def _query_drawing(ctx: AssistantContext, call: QueryDrawing) -> dict[str, Any]:
    from syntera.drawings.graph import dimensions_of, find_by_tag, label_of

    graph = _graph(ctx, call.drawing)
    if call.tag:
        nodes = find_by_tag(graph, call.tag)
        return {"tag": call.tag, "entities": [
            {"handle": node.handle, "type": node.type, "layer": node.layer, "kind": node.kind,
             "labels": label_of(graph, node.handle),
             "dimensions": [d.measurement for d in dimensions_of(graph, node.handle)]}
            for node in nodes
        ]}  # fmt: skip
    if call.handle:
        try:
            node = graph.node(call.handle)
        except KeyError as error:
            raise ToolError(f"no entity {call.handle}") from error
        return {**node.model_dump(mode="json"), "label_texts": label_of(graph, node.handle)}
    return {"title_block": graph.title_block, "entities": len(graph.nodes)}


def _explain(ctx: AssistantContext, call: Explain) -> dict[str, Any]:
    from syntera.knowledge.explain import (
        ExplainError,
        back_calculate_velocity,
        explain_entity,
        explain_value,
    )

    try:
        if call.path:
            result = explain_value(ctx.design, call.path, call.mode, ctx.kb)
        elif call.drawing and call.handle:
            graph = _graph(ctx, call.drawing)
            if call.flow_m3_per_h is not None:
                result = back_calculate_velocity(graph, call.handle, call.drawing,
                                                 call.flow_m3_per_h, call.wall_mm,
                                                 mode=call.mode, kb=ctx.kb)  # fmt: skip
            else:
                result = explain_entity(graph, call.handle, call.drawing, call.mode)
        else:
            raise ToolError("give a path, or a drawing and a handle")
    except ExplainError as error:
        raise ToolError(str(error)) from error
    return result.model_dump(mode="json")


def _search(ctx: AssistantContext, call: Search) -> dict[str, Any]:
    from syntera.knowledge.retrieval import build_index

    index = build_index(ctx.kb, [("design", ctx.design.rationale)], sorted(ctx.graphs.items()))
    return {"hits": [hit.model_dump(mode="json") for hit in index.search(call.query, call.limit)]}


def _verdict_summary(report) -> dict[str, Any]:
    return {
        "overall_pass": report.overall_pass,
        "lines": {line.line: line.overall_pass for line in report.lines},
        "failure_reasons": report.failure_reasons,
        "source": "exact geometry checks (route-design)",
    }


def _regenerate(ctx: AssistantContext, call: Regenerate | RunChecks) -> dict[str, Any]:
    from syntera.multiline import route_design

    folder = ctx.output / f"run-{len(ctx.reports) + 1}"
    try:
        result = route_design(ctx.design, folder)
    except DesignError as error:
        raise ToolError(str(error)) from error
    ctx.reports.append(result.report)
    if isinstance(call, Regenerate):
        ctx.design = result.design
    return {**_verdict_summary(result.report), "output": str(folder)}


def _proposal_id(design: DesignDocument, changes: ChangeSet) -> str:
    from syntera.design import design_sha256

    payload = json.dumps([design_sha256(design), changes.model_dump(mode="json")], sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


# Edits an assistant may not propose: outputs (routes), explanations (rationale), and the
# acceptance thresholds that define the checks themselves.
ASSISTANT_FORBIDDEN_OPS = frozenset({"set_routes", "set_rationale"})


def guard_changes(changes: ChangeSet) -> None:
    """Refuse change sets that would write outputs or loosen the checks."""
    for number, edit in enumerate(changes.edits, start=1):
        if edit.op in ASSISTANT_FORBIDDEN_OPS:
            raise ToolError(f"edit {number}: {edit.op} is not available to the assistant")
        if edit.op == "set_parameter" and (
            ".acceptance." in f".{edit.path}." or edit.path.split(".")[0] == "routes"
        ):
            raise ToolError(f"edit {number}: acceptance thresholds can only be set by the user")
        if edit.op == "add_line" and edit.line.analysis is not None:
            raise ToolError(
                f"edit {number}: a new line's analysis and acceptance must come from the user"
            )


def _propose(ctx: AssistantContext, call: ProposeChange) -> dict[str, Any]:
    from syntera.design import design_sha256

    guard_changes(call.changes)
    try:
        changed, _ = apply_change_set(ctx.design, call.changes)
    except DesignError as error:
        raise ToolError(f"the change set cannot be applied: {error}") from error
    proposal_id = _proposal_id(ctx.design, call.changes)
    ctx.proposals[proposal_id] = Proposal(proposal_id, call.changes, design_sha256(ctx.design))
    diff = diff_designs(ctx.design, changed)
    return {
        "proposal_id": proposal_id,
        "applied": False,
        "diff": diff.model_dump(mode="json"),
        "next": "show the diff to the user; apply only after they confirm",
    }


def _apply(ctx: AssistantContext, call: ApplyChange) -> dict[str, Any]:
    from syntera.design import design_sha256

    if not call.user_confirmed:
        raise ToolError("the user has not confirmed this proposal; nothing was applied")
    proposal = ctx.proposals.get(call.proposal_id)
    if proposal is None:
        raise ToolError(f"unknown proposal {call.proposal_id}")
    if proposal.design_sha256 != design_sha256(ctx.design):
        raise ToolError("the design changed since this proposal; propose it again")
    changed, revert = apply_change_set(ctx.design, proposal.changes)
    ctx.history.append(ctx.design)
    ctx.design = changed
    del ctx.proposals[call.proposal_id]
    return {"applied": True, "revert": revert.model_dump(mode="json"),
            "next": "run regenerate to route and verify the changed design"}  # fmt: skip


def _compare(ctx: AssistantContext, call: Compare) -> dict[str, Any]:
    if not ctx.history:
        raise ToolError("no earlier version to compare with")
    diff = diff_designs(ctx.history[-1], ctx.design)
    if len(ctx.reports) >= 2:
        diff = diff.model_copy(
            update={
                "verdicts": verdict_changes(ctx.reports[-2], ctx.reports[-1]),
                "regenerated": True,
            }  # fmt: skip
        )
    return diff.model_dump(mode="json")


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_model: type[StrictModel]
    run: Callable[[AssistantContext, Any], dict[str, Any]]
    changes_design: bool = False

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_model.model_json_schema(),
        }


TOOLS: tuple[Tool, ...] = (
    Tool("query_design", "Read the design: summary, a line, equipment, nozzle or a value "
         "with its source.", QueryDesign, _query_design),
    Tool("query_drawing", "Find drawing entities by tag or describe one entity; labels and "
         "dimensions are associations by proximity.", QueryDrawing, _query_drawing),
    Tool("explain", "Explain why a design value is what it is, or infer what a drawing "
         "entity is (always labelled inference).", Explain, _explain),
    Tool("search", "Keyword search over knowledge, design rationale and drawings.", Search,
         _search),
    Tool("run_checks", "Route every line and run the exact geometry checks; the only "
         "source of verdicts.", RunChecks, _regenerate),
    Tool("propose_change", "Validate a change set and show its diff; nothing is applied.",
         ProposeChange, _propose),
    Tool("apply_change", "Apply a proposal after the user confirmed it.", ApplyChange,
         _apply, changes_design=True),
    Tool("regenerate", "Route and verify the design and store the routes in it.",
         Regenerate, _regenerate, changes_design=True),
    Tool("compare", "Compare the current design with its previous version.", Compare,
         _compare),
)  # fmt: skip
TOOLS_BY_NAME = {tool.name: tool for tool in TOOLS}


def tool_schemas() -> list[dict[str, Any]]:
    return [tool.schema() for tool in TOOLS]


def call_tool(ctx: AssistantContext, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Validate and run one tool call; every call (and refusal) is logged on the context."""
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        raise ToolError(f"unknown tool {name}; tools: {sorted(TOOLS_BY_NAME)}")
    try:
        call = tool.input_model.model_validate(arguments)
    except ValueError as error:
        ctx.log.append({"tool": name, "arguments": arguments, "error": str(error)})
        raise ToolError(f"invalid arguments for {name}: {error}") from error
    try:
        result = tool.run(ctx, call)
    except ToolError as error:
        ctx.log.append({"tool": name, "arguments": arguments, "error": str(error)})
        raise
    ctx.log.append({"tool": name, "arguments": arguments, "ok": True})
    return result


def schema_field_names(schema: dict[str, Any]) -> set[str]:
    """Every property name anywhere in a JSON schema (including $defs)."""
    names: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.get("properties", {}).items():
                names.add(key)
                walk(value)
            for key, value in node.items():
                if key != "properties":
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return names
