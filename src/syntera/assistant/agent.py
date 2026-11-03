"""LLM provider interface and a bounded agent loop over the assistant tools (roadmap F2).

A :class:`Provider` turns a conversation into the next assistant message, which may ask
for tool calls. :func:`run_agent` executes those calls through
:func:`syntera.assistant.tools.call_tool`, feeds the results back, and stops at an
answer or a step limit. Every message is kept in a transcript (written as JSON lines).

``ScriptedProvider`` replays fixed replies, so tests and CI never need a model or a key.
``AnthropicProvider`` calls the Anthropic Messages API and is available only when
``ANTHROPIC_API_KEY`` is set. Whatever the provider says, verdicts come only from the
checks: an answer that talks about passing or failing without a check having run in the
conversation gets an explicit warning appended.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from syntera.assistant.tools import AssistantContext, ToolError, call_tool, tool_schemas

TRANSCRIPT_FILE = "transcript.jsonl"
DEFAULT_MAX_STEPS = 8
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-4-5"
VERDICT_TOOLS = frozenset({"run_checks", "regenerate"})
VERDICT_TALK = re.compile(r"\b(pass(es|ed)?|fail(s|ed)?|verified|compliant|safe)\b", re.I)
UNCHECKED_WARNING = (
    "Note: no checks were run in this conversation, so nothing above is a verdict. "
    "Only run_checks or regenerate (the exact geometry checks) decide pass or fail."
)

SYSTEM_PROMPT = """You are Syntera's engineering assistant for tube and pipe layouts.
Rules you must follow:
- Use only the tools. Never invent values; read them with query_design, query_drawing or
  explain, and cite rationale paths, rule ids or drawing handles you used.
- Anything about a foreign drawing is inference; say so.
- You cannot decide whether a design passes. Only run_checks or regenerate report
  verdicts, and you must quote them, never predict them.
- To change a design, call propose_change and show the diff. Apply it only after the user
  explicitly confirms, with apply_change and user_confirmed true.
- Rule and material tables are synthetic demonstration data, not design-code values.
Answer briefly in plain language."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TextBlock(StrictModel):
    type: Literal["text"] = "text"
    text: str


class ToolUseBlock(StrictModel):
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any]


class ToolResultBlock(StrictModel):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str
    is_error: bool = False


Block = TextBlock | ToolUseBlock | ToolResultBlock


class Message(StrictModel):
    role: Literal["user", "assistant"]
    content: list[Block]


class ProviderReply(StrictModel):
    content: list[TextBlock | ToolUseBlock]
    stop_reason: Literal["end_turn", "tool_use", "max_tokens", "stop_sequence"] = "end_turn"


class Provider(Protocol):
    name: str

    def complete(
        self, system: str, messages: Sequence[Message], tools: list[dict[str, Any]]
    ) -> ProviderReply: ...


class ProviderError(RuntimeError):
    """The provider could not produce a reply."""


class ScriptedProvider:
    """Replays fixed replies in order; records every request. For tests and CI."""

    name = "scripted"

    def __init__(self, replies: Sequence[ProviderReply]):
        self.replies = list(replies)
        self.requests: list[list[Message]] = []

    def complete(
        self, system: str, messages: Sequence[Message], tools: list[dict[str, Any]]
    ) -> ProviderReply:
        self.requests.append(list(messages))
        if not self.replies:
            raise ProviderError("the script has no more replies")
        return self.replies.pop(0)


class AnthropicProvider:
    """Anthropic Messages API over HTTPS; needs ``ANTHROPIC_API_KEY``."""

    name = "anthropic"

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, max_tokens: int = 1024,
                 timeout_s: float = 60.0):  # fmt: skip
        if not api_key:
            raise ProviderError("ANTHROPIC_API_KEY is not set")
        self.api_key, self.model = api_key, model
        self.max_tokens, self.timeout_s = max_tokens, timeout_s

    def request_body(
        self, system: str, messages: Sequence[Message], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "tools": tools,
            "messages": [message.model_dump(mode="json") for message in messages],
        }

    @staticmethod
    def parse(body: dict[str, Any]) -> ProviderReply:
        blocks: list[TextBlock | ToolUseBlock] = []
        for block in body.get("content", []):
            if block.get("type") == "text":
                blocks.append(TextBlock(text=block["text"]))
            elif block.get("type") == "tool_use":
                blocks.append(ToolUseBlock(id=block["id"], name=block["name"],
                                           input=block.get("input") or {}))  # fmt: skip
        reason = body.get("stop_reason") or "end_turn"
        if reason not in {"end_turn", "tool_use", "max_tokens", "stop_sequence"}:
            reason = "end_turn"
        return ProviderReply(content=blocks, stop_reason=reason)

    def complete(
        self, system: str, messages: Sequence[Message], tools: list[dict[str, Any]]
    ) -> ProviderReply:
        request = urllib.request.Request(
            ANTHROPIC_URL,
            data=json.dumps(self.request_body(system, messages, tools)).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": ANTHROPIC_VERSION,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                return self.parse(json.loads(response.read().decode("utf-8")))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise ProviderError(f"Anthropic API call failed: {error}") from error


def provider_from_env(environ: dict[str, str] | None = None) -> AnthropicProvider | None:
    """The Anthropic provider when a key is configured, else ``None`` (no network use)."""
    env = os.environ if environ is None else environ
    key = env.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return None
    return AnthropicProvider(key, model=env.get("SYNTERA_ANTHROPIC_MODEL", DEFAULT_MODEL))


class AgentResult(StrictModel):
    question: str
    answer: str
    stopped: Literal["answer", "step_limit", "provider_error"]
    steps: int
    tool_calls: list[str] = Field(default_factory=list)
    checks_run: bool = False
    transcript: list[Message]


def _text(blocks: Sequence[Block]) -> str:
    return "\n".join(block.text for block in blocks if isinstance(block, TextBlock)).strip()


def run_agent(
    provider: Provider,
    ctx: AssistantContext,
    question: str,
    max_steps: int = DEFAULT_MAX_STEPS,
    history: Sequence[Message] = (),
    transcript_path: Path | None = None,
) -> AgentResult:
    """Ask, run requested tools, feed results back, until an answer or ``max_steps``."""
    messages: list[Message] = [*history, Message(role="user", content=[TextBlock(text=question)])]
    tools = tool_schemas()
    called: list[str] = []
    stopped: Literal["answer", "step_limit", "provider_error"] = "step_limit"
    answer = ""
    steps = 0
    while steps < max_steps:
        steps += 1
        try:
            reply = provider.complete(SYSTEM_PROMPT, messages, tools)
        except ProviderError as error:
            stopped, answer = "provider_error", f"The assistant is unavailable: {error}"
            break
        messages.append(Message(role="assistant", content=list(reply.content)))
        uses = [block for block in reply.content if isinstance(block, ToolUseBlock)]
        if not uses:
            stopped, answer = "answer", _text(reply.content)
            break
        results: list[Block] = []
        for use in uses:
            called.append(use.name)
            try:
                output = call_tool(ctx, use.name, use.input)
                content = json.dumps(output, sort_keys=True)
                results.append(ToolResultBlock(tool_use_id=use.id, content=content))
            except ToolError as error:
                results.append(ToolResultBlock(tool_use_id=use.id, content=str(error),
                                               is_error=True))  # fmt: skip
        messages.append(Message(role="user", content=results))
    if stopped == "step_limit":
        answer = (f"Stopped after {max_steps} steps without an answer. "
                  "Ask a narrower question.")  # fmt: skip
    checks_run = any(name in VERDICT_TOOLS for name in called)
    if stopped == "answer" and VERDICT_TALK.search(answer) and not checks_run:
        answer = f"{answer}\n\n{UNCHECKED_WARNING}"
    result = AgentResult(
        question=question,
        answer=answer,
        stopped=stopped,
        steps=steps,
        tool_calls=called,
        checks_run=checks_run,
        transcript=messages,
    )
    if transcript_path is not None:
        write_transcript(result, transcript_path)
    return result


def write_transcript(result: AgentResult, path: Path) -> Path:
    """One JSON object per message, then a summary line; no timestamps (deterministic)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(m.model_dump(mode="json"), sort_keys=True) for m in result.transcript]
    summary = result.model_dump(mode="json", exclude={"transcript"})
    lines.append(json.dumps({"summary": summary}, sort_keys=True))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
