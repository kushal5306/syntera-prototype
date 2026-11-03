"""Assistant evaluation suite: questions with expected facts and citations (roadmap F5).

Cases live in ``eval_cases.yaml``. In ``scripted`` mode a deterministic provider calls
each case's hinted tool and answers from the tool's result, so CI checks that the tools
produce the facts and citations an answer needs. In ``anthropic`` mode a real model
answers (needs ``ANTHROPIC_API_KEY``). Every run reports per-question results.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from syntera.assistant.agent import (
    Message,
    Provider,
    ProviderReply,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    provider_from_env,
    run_agent,
)
from syntera.assistant.tools import AssistantContext
from syntera.design import load_design_input

CASES_FILE = Path(__file__).with_name("eval_cases.yaml")
PROJECT_ROOT = Path(__file__).parents[3]
EVAL_FILE = "assistant_eval.json"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvalCase(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    question: str
    design: str
    drawing: str | None = None
    tool: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    answer: str | None = None
    facts: list[str]
    citations: list[str]


class CaseResult(StrictModel):
    id: str
    passed: bool
    missing_facts: list[str]
    missing_citations: list[str]
    tool_calls: list[str]
    answer: str


class EvalReport(StrictModel):
    provider: str
    total: int
    passed: int
    cases: list[CaseResult]


def load_cases(path: Path = CASES_FILE) -> list[EvalCase]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    cases = [EvalCase.model_validate(item) for item in raw]
    ids = [case.id for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("evaluation case ids must be unique")
    return cases


# ---- fixture drawings --------------------------------------------------------------------


def pump_plan(folder: Path):
    """A small plan: pump outline P-101 with a width dimension, and line L-001 labelled OD."""
    import ezdxf

    from syntera.drawings.graph import graph_from_file

    doc = ezdxf.new("R2010", setup=True)
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (600, 0), (600, 400), (0, 400)], close=True,
                       dxfattribs={"layer": "EQUIPMENT"})  # fmt: skip
    msp.add_text("PUMP P-101", height=20).set_placement((250, 200))
    msp.add_linear_dim(base=(0, -50), p1=(0, 0), p2=(600, 0)).render()
    msp.add_line((600, 200), (1600, 200), dxfattribs={"layer": "PIPE"})
    msp.add_text("L-001 OD 60.3", height=10).set_placement((1000, 215))
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "pump_plan.dxf"
    doc.saveas(path)
    return graph_from_file(path)


FIXTURES = {"pump_plan": pump_plan}


# ---- scripted provider -------------------------------------------------------------------


def render_result(name: str, data: dict[str, Any]) -> str:
    """A deterministic plain-text answer built only from a tool result."""
    if name == "explain":
        parts = [data["headline"], *data["steps"]]
        if data.get("rule_id"):
            parts.append(f"Rule: {data['rule_id']}.")
        return " ".join(parts)
    if name == "query_drawing":
        lines = []
        for entity in data.get("entities", []):
            dims = ", ".join(f"{value:g}" for value in entity["dimensions"] if value is not None)
            labels = "; ".join(entity["labels"])
            where = f"{entity['type']} {entity['handle']} on {entity['layer']}"
            lines.append(f"{where} labelled {labels} (dimensions: {dims or 'none'}).")
        return " ".join(lines) or "Nothing on the drawing carries that tag."
    if name == "search":
        return " ".join(f"{hit['title']} ({hit['target']})." for hit in data.get("hits", []))
    return json.dumps(data, sort_keys=True)


class ToolEchoProvider:
    """Calls one tool, then answers with :func:`render_result`; or gives a fixed answer."""

    name = "scripted"

    def __init__(self, tool: str | None, arguments: dict[str, Any], answer: str | None):
        self.tool, self.arguments, self.answer = tool, arguments, answer

    def complete(self, system: str, messages: Sequence[Message], tools) -> ProviderReply:
        if self.tool is None:
            return ProviderReply(content=[TextBlock(text=self.answer or "")])
        last = messages[-1].content[0]
        if isinstance(last, ToolResultBlock):
            text = last.content if last.is_error else render_result(
                self.tool, json.loads(last.content)
            )  # fmt: skip
            return ProviderReply(content=[TextBlock(text=text)])
        return ProviderReply(
            content=[ToolUseBlock(id="eval-1", name=self.tool, input=self.arguments)],
            stop_reason="tool_use",
        )


def _resolve(arguments: dict[str, Any], graphs: dict[str, Any]) -> dict[str, Any]:
    from syntera.drawings.graph import find_by_tag

    resolved = {}
    for key, value in arguments.items():
        if isinstance(value, str) and value.startswith("$tag:"):
            tag = value.removeprefix("$tag:")
            nodes = [node for graph in graphs.values() for node in find_by_tag(graph, tag)]
            if not nodes:
                raise ValueError(f"no entity tagged {tag} in the fixture drawings")
            value = nodes[0].handle
        resolved[key] = value
    return resolved


# ---- running -----------------------------------------------------------------------------


def run_case(case: EvalCase, provider: Provider | None, output: Path) -> CaseResult:
    from syntera.web.assistant import citations

    graphs = {}
    if case.drawing:
        graphs[case.drawing] = FIXTURES[case.drawing](output / "fixtures")
    ctx = AssistantContext(
        design=load_design_input(PROJECT_ROOT / case.design), output=output / case.id,
        graphs=graphs,
    )  # fmt: skip
    if provider is None:
        provider = ToolEchoProvider(case.tool, _resolve(case.arguments, graphs), case.answer)
    result = run_agent(provider, ctx, case.question)
    answer = result.answer
    missing_facts = [fact for fact in case.facts if fact.lower() not in answer.lower()]
    cited = [citation.id for citation in citations(result)]
    missing = [want for want in case.citations if not any(c.startswith(want) for c in cited)]
    return CaseResult(
        id=case.id,
        passed=not missing_facts and not missing and result.stopped == "answer",
        missing_facts=missing_facts,
        missing_citations=missing,
        tool_calls=result.tool_calls,
        answer=answer,
    )


def run_suite(
    output: Path,
    mode: Literal["scripted", "anthropic"] = "scripted",
    cases: Sequence[EvalCase] | None = None,
) -> EvalReport:
    provider = None
    if mode == "anthropic":
        provider = provider_from_env()
        if provider is None:
            raise ValueError("set ANTHROPIC_API_KEY to evaluate a real model")
    results = [run_case(case, provider, output) for case in (cases or load_cases())]
    return EvalReport(
        provider=mode,
        total=len(results),
        passed=sum(result.passed for result in results),
        cases=results,
    )
