"""Who writes the chat's side of the conversation.

* :class:`ClaudeChatModel` calls the Claude API through the official SDK when
  ``ANTHROPIC_API_KEY`` is set. Assistant content is kept exactly as returned (thinking
  blocks included) so tool loops replay it unchanged.
* :class:`BasicChatModel` needs no key or network: it recognises uploads, the sample
  request and simple "from A to B" requests, calls the same tools, and answers with the
  tools' own plain summaries. It keeps Syntera usable, and CI deterministic, without a model.
* :class:`ScriptedChatModel` replays fixed replies for tests.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

DEFAULT_CHAT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
Block = dict[str, Any]
ChatMessage = dict[str, Any]


@dataclass
class ModelReply:
    content: list[Block]
    stop_reason: str = "end_turn"


class ChatModelError(RuntimeError):
    """The model could not produce a reply; the message is safe to show the person."""


class ChatModel(Protocol):
    name: str
    label: str

    def complete(
        self, system: str, messages: Sequence[ChatMessage], tools: list[dict[str, Any]]
    ) -> ModelReply: ...


class ScriptedChatModel:
    """Replays fixed replies in order and records every request. For tests."""

    name = "scripted"
    label = "Scripted test model"

    def __init__(self, replies: Sequence[ModelReply]):
        self.replies = list(replies)
        self.requests: list[list[ChatMessage]] = []

    def complete(self, system, messages, tools) -> ModelReply:
        self.requests.append([dict(message) for message in messages])
        if not self.replies:
            raise ChatModelError("the script has no more replies")
        return self.replies.pop(0)


class ClaudeChatModel:
    """Claude through the Anthropic SDK: adaptive thinking, prompt caching, fallbacks."""

    name = "claude"

    def __init__(self, api_key: str, model: str = DEFAULT_CHAT_MODEL, effort: str = "medium",
                 client: Any = None):  # fmt: skip
        if client is None:
            import anthropic

            client = anthropic.Anthropic(api_key=api_key, timeout=300.0, max_retries=2)
        self.client, self.model, self.effort = client, model, effort
        self.label = f"Claude ({model})"

    def complete(self, system, messages, tools) -> ModelReply:
        import anthropic

        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=system,
                tools=tools,
                messages=list(messages),
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort},
                cache_control={"type": "ephemeral"},
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except anthropic.AuthenticationError as error:
            raise ChatModelError(
                "The Claude API key was rejected. Check ANTHROPIC_API_KEY on the Syntera server."
            ) from error
        except anthropic.PermissionDeniedError as error:
            raise ChatModelError(f"The Claude API key cannot use {self.model}.") from error
        except anthropic.NotFoundError as error:
            raise ChatModelError(
                f"Model {self.model} was not found; check SYNTERA_ANTHROPIC_MODEL."
            ) from error
        except anthropic.RateLimitError as error:
            raise ChatModelError("Claude is rate limited right now; try again in a minute.") from (
                error
            )
        except anthropic.APIStatusError as error:
            raise ChatModelError(f"Claude returned an error ({error.status_code}).") from error
        except anthropic.APIConnectionError as error:
            raise ChatModelError(
                "The Syntera server could not reach the Claude API; check its internet access."
            ) from error
        content = [block.model_dump(mode="json", exclude_none=True) for block in response.content]
        return ModelReply(content=content, stop_reason=response.stop_reason or "end_turn")


def chat_model_from_env(environ: dict[str, str] | None = None) -> ChatModel:
    """Claude when a key is configured, otherwise the rule-based basic mode."""
    env = os.environ if environ is None else environ
    key = env.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return ClaudeChatModel(
            key,
            model=env.get("SYNTERA_ANTHROPIC_MODEL", "").strip() or DEFAULT_CHAT_MODEL,
            effort=env.get("SYNTERA_ANTHROPIC_EFFORT", "").strip() or "medium",
        )
    return BasicChatModel()


# ---- basic mode ------------------------------------------------------------------------

TYPE_WORDS = (
    ("plate heat exchanger", "plate_heat_exchanger"),
    ("heat exchanger", "heat_exchanger"),
    ("exchanger", "heat_exchanger"),
    ("cooler", "heat_exchanger"),
    ("condenser", "heat_exchanger"),
    ("compressor", "compressor"),
    ("column", "column"),
    ("tower", "column"),
    ("separator", "separator"),
    ("filter", "filter"),
    ("mixer", "mixer"),
    ("drum", "drum"),
    ("vessel", "vessel"),
    ("reactor", "vessel"),
    ("tank", "tank"),
    ("pump", "pump"),
)
FILE_NOTE = re.compile(r"\[attached file (f[0-9a-f]{16}): ([^\]]+?) \((drawing|pid|step)\)\]")
KIND_TOOL = {"drawing": "check_drawing", "pid": "pid_to_3d", "step": "audit_step"}
HELP = (
    "I can do three things without an AI key: check a drawing or 3D model you attach "
    "(DWG, DXF, DEXPI P&ID or STEP), check the sample plant-room drawing, or design a "
    'simple plant from a sentence like "pump P-1 from tank T-1 to tank T-2, DN50". '
    "For free conversation, add a Claude API key to the Syntera server."
)


def _equipment_type(name: str) -> str:
    lowered = name.lower()
    for word, kind in TYPE_WORDS:
        if word in lowered:
            return kind
    return "other"


def _tag(phrase: str) -> str:
    phrase = phrase.strip(" .,;:'\"")
    match = re.search(r"\b([A-Za-z]{1,4}-?\d{1,4}[A-Za-z]?)\b", phrase)
    if match:
        return match.group(1).upper()
    words = [w for w in re.findall(r"[A-Za-z]+", phrase) if w.lower() not in {"the", "a", "an"}]
    return " ".join(words[-2:]).title() if words else phrase.title()


def parse_plant_request(text: str) -> dict[str, Any] | None:
    """Turn simple "X from A to B" sentences into design_plant arguments, else None."""
    lines: list[dict[str, Any]] = []
    equipment: dict[str, dict[str, Any]] = {}
    default_dn = None
    match = re.search(r"\bDN\s?(\d{2,3})\b", text, re.I)
    if match:
        default_dn = int(match.group(1))

    def add(phrase: str) -> str:
        tag = _tag(phrase)
        key = tag.lower()
        if key in {"outside", "outside plant", "battery limit"}:
            return "outside"
        if key not in equipment:
            equipment[key] = {"tag": tag, "type": _equipment_type(phrase + " " + tag)}
        return equipment[key]["tag"]

    for clause in re.split(r"[;\n]|\.\s|\band then\b|,\s*then\b", text):
        found = re.search(
            r"(?:(?P<via>\b(?:a |the )?[\w -]*?\b(?:pump|compressor|filter|exchanger|cooler)"
            r"\b[\w -]*?)\s+)?\bfrom\s+(?P<a>.+?)\s+(?:to|into)\s+(?P<b>[^,]+?)"
            r"(?:,|\bDN\b|\bwith\b|\bin\b|\bfit\b|$)",
            clause,
            re.I,
        )
        if not found:
            continue
        dn_match = re.search(r"\bDN\s?(\d{2,3})\b", clause, re.I)
        dn = int(dn_match.group(1)) if dn_match else default_dn
        start, end = add(found.group("a")), add(found.group("b"))
        via = found.group("via")
        named = re.search(
            r"\b(?:with|using|via|through)\s+(?:a\s+|the\s+)?(?P<v>(?:pump|compressor|filter|"
            r"cooler|heat exchanger|exchanger)(?:\s+[A-Za-z]{1,4}-?\d{1,4}[A-Za-z]?)?)",
            clause,
            re.I,
        )
        if named:
            via = named.group("v")
        if via and via.strip().lower() not in {"pipe", "line"}:
            middle = add(via)
            lines.append({"from_tag": start, "to_tag": middle, "dn": dn})
            lines.append({"from_tag": middle, "to_tag": end, "dn": dn})
        else:
            lines.append({"from_tag": start, "to_tag": end, "dn": dn})
    if not lines or not equipment:
        return None
    request: dict[str, Any] = {
        "title": "Plant from chat",
        "equipment": list(equipment.values()),
        "lines": [{k: v for k, v in line.items() if v is not None} for line in lines],
    }
    room = re.search(r"(\d+(?:\.\d+)?)\s*(?:m|metres|meters)?\s*(?:x|by)\s*(\d+(?:\.\d+)?)\s*"
                     r"(?:m|metres|meters)\b", text, re.I)  # fmt: skip
    if room:
        request["room"] = {"length_m": float(room.group(1)), "width_m": float(room.group(2))}
    return request


def _last_user_text(messages: Sequence[ChatMessage]) -> str:
    for message in reversed(messages):
        if message["role"] != "user":
            continue
        content = message["content"]
        if isinstance(content, str):
            return content
        texts = [b.get("text", "") for b in content if b.get("type") == "text"]
        if texts:
            return "\n".join(texts)
    return ""


class BasicChatModel:
    """Rule-based stand-in for a language model; deterministic and offline."""

    name = "basic"
    label = "Basic mode (no AI key)"

    def complete(self, system, messages, tools) -> ModelReply:
        last = messages[-1]
        results = [
            b for b in last["content"] if isinstance(b, dict) and b.get("type") == "tool_result"
        ] if isinstance(last["content"], list) else []  # fmt: skip
        if results:
            return self._after_tools(messages, results)
        text = _last_user_text(messages)
        notes = FILE_NOTE.findall(text)
        if notes:
            file_id, _, kind = notes[-1]
            return self._call(KIND_TOOL[kind], {"file_id": file_id}, len(messages))
        lowered = text.lower()
        if "sample" in lowered or "example" in lowered or "demo" in lowered:
            return self._call("use_sample", {"sample": "plant_room_drawing"}, len(messages))
        request = parse_plant_request(text)
        if request is not None:
            return self._call("design_plant", request, len(messages))
        return ModelReply([{"type": "text", "text": HELP}])

    @staticmethod
    def _call(name: str, arguments: dict[str, Any], turn: int) -> ModelReply:
        return ModelReply(
            [{"type": "tool_use", "id": f"basic_{turn}_{name}", "name": name, "input": arguments}],
            stop_reason="tool_use",
        )

    def _after_tools(self, messages, results) -> ModelReply:
        result = results[-1]
        content = result.get("content", "")
        if result.get("is_error"):
            return ModelReply([{"type": "text", "text": f"I could not do that: {content}"}])
        data = json.loads(content) if isinstance(content, str) else {}
        if "file" in data and data.get("next", "").startswith("call check_drawing"):
            return self._call("check_drawing", {"file_id": data["file"]["id"]}, len(messages))
        summary = data.get("plain_summary") or "Done."
        problems = data.get("problems") or [
            {"title": p["message"], "fix": ""}
            for p in data.get("design_audit", {}).get("problems", [])
        ]
        lines = [summary]
        if problems:
            lines.append("")
            lines.append("What to fix:")
            for problem in problems[:6]:
                fix = f" Fix: {problem['fix']}" if problem.get("fix") else ""
                lines.append(f"- {problem['title']}.{fix}")
        lines.append("")
        lines.append("The 3D view shows the result; the downloads are under it.")
        return ModelReply([{"type": "text", "text": "\n".join(lines)}])
