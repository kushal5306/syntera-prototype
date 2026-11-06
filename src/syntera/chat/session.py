"""Conversations: the agent loop, the event log the browser shows, and background turns.

A turn appends the person's message, then lets the model call tools until it answers or
hits the step limit. Long tools (a drawing check can take minutes) run on a background
thread; the browser polls the session's events, which say what Syntera is doing now.
Verdicts in replies come only from tool results; a reply that talks about passing or
failing when no checking tool ran this turn gets a note saying so.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from syntera.chat.models import ChatMessage, ChatModel, ChatModelError
from syntera.chat.tools import (
    ChatFile,
    ChatToolError,
    ChatWorkspace,
    chat_tool_schemas,
    run_chat_tool,
)

MAX_STEPS = 10
MAX_TURNS = 40
SESSION_ID = re.compile(r"^c[0-9a-f]{20}$")
CHECKING_TOOLS = frozenset({"design_plant", "check_drawing", "pid_to_3d", "audit_step"})
VERDICT_TALK = re.compile(r"\b(pass(es|ed)?|fail(s|ed)?|verified|compliant|clash(es)?)\b", re.I)
UNCHECKED_NOTE = (
    "(No check ran for this answer, so treat any pass or fail above as a guess, not a result.)"
)

SYSTEM_PROMPT = """You are Syntera, a piping and plant layout assistant for people who are \
not engineers. They describe what they need in everyday words or attach a file, and you do \
the engineering with your tools.

How to work:
- Act first. Use sensible defaults instead of asking: typical equipment sizes, DN50 pipe \
when no size is given, one pipe per connection. Ask one short question only when you \
cannot start at all, for example when it is unclear what connects to what.
- A message may carry notes like [attached file f0123456789abcdef: name.dxf (drawing)]. \
Use check_drawing for drawings, pid_to_3d for P&ID exports and audit_step for STEP models. \
If someone wants to try Syntera with no file, use use_sample, then check_drawing.
- To design something new, call design_plant. Give equipment short tags (T-1, P-1). A \
pump that moves liquid from A to B is two pipe runs: A to the pump, the pump to B. Use \
"outside" for a pipe that comes in from, or leaves to, outside the plant. If a room size \
is given, pass it so Syntera says whether the layout fits.
- To change a design, call design_plant again with the changed description.

How to answer:
- Lead with the result in one or two sentences: does it work, and what it costs or saves.
- Then at most a few short bullet points: problems found and how to fix each, and what \
Syntera changed and why. Name lines and equipment by their tags.
- Plain words. Explain any engineering term in a few words the first time (DN50 means a \
pipe of about 50 mm bore). Metres for lengths, mm for pipe sizes.
- Every number, verdict and saving must come from a tool result in this conversation. \
Never predict whether a design passes; the tools decide. Say which sizes were assumed.
- The 3D view and downloads appear next to the chat; mention them briefly instead of \
describing geometry at length.
- Cost figures use placeholder rates and design tables are demonstration data; say so if \
someone treats them as a quote. Layouts are proposals for an engineer to review."""


@dataclass
class ChatSession:
    id: str
    messages: list[ChatMessage] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    busy: bool = False
    turns: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def emit(self, event: dict[str, Any]) -> dict[str, Any]:
        event = {"id": len(self.events) + 1, "time": round(time.time(), 3), **event}
        self.events.append(event)
        return event

    def update(self, event: dict[str, Any], **changes: Any) -> None:
        event.update(changes)

    def snapshot(self, since: int = 0) -> dict[str, Any]:
        return {
            "id": self.id,
            "busy": self.busy,
            "events": [dict(e) for e in self.events if e["id"] > since],
            "turns": self.turns,
        }


def user_content(text: str, files: list[ChatFile]) -> str:
    notes = [f"[attached file {f.id}: {f.filename} ({f.kind})]" for f in files]
    return "\n".join([text.strip(), *notes]).strip()


def _texts(content: list[dict[str, Any]]) -> str:
    return "\n\n".join(b["text"] for b in content if b.get("type") == "text" and b.get("text"))


def run_turn(
    session: ChatSession,
    model: ChatModel,
    ws: ChatWorkspace,
    text: str,
    files: list[ChatFile],
    max_steps: int = MAX_STEPS,
) -> dict[str, Any]:
    """One exchange: the person's message, tool calls, and the reply event."""
    session.messages.append({"role": "user", "content": user_content(text, files)})
    tools = chat_tool_schemas()
    views: list[dict[str, Any]] = []
    checked = False
    answer = ""
    for _ in range(max_steps):
        try:
            reply = model.complete(SYSTEM_PROMPT, session.messages, tools)
        except ChatModelError as error:
            return session.emit({"type": "error", "text": str(error)})
        session.messages.append({"role": "assistant", "content": reply.content})
        if reply.stop_reason == "refusal":
            answer = "I can't help with that request. I can design, check or audit piping."
            break
        uses = [b for b in reply.content if b.get("type") == "tool_use"]
        if not uses:
            answer = _texts(reply.content)
            break
        results = []
        for use in uses:
            activity = session.emit({"type": "activity", "tool": use["name"],
                                     "text": "Starting…", "status": "running"})  # fmt: skip
            started = time.monotonic()

            def progress(message: str, activity: dict = activity) -> None:
                session.update(activity, text=message)

            try:
                outcome = run_chat_tool(ws, use["name"], use.get("input") or {}, progress)
            except ChatToolError as error:
                session.update(activity, status="failed", text=str(error))
                results.append({"type": "tool_result", "tool_use_id": use["id"],
                                "content": str(error), "is_error": True})  # fmt: skip
                continue
            except Exception as error:  # a crash in a workflow must not kill the chat
                traceback.print_exc()
                message = f"Syntera hit an internal error: {type(error).__name__}: {error}"
                session.update(activity, status="failed", text=message)
                results.append({"type": "tool_result", "tool_use_id": use["id"],
                                "content": message, "is_error": True})  # fmt: skip
                continue
            checked = checked or use["name"] in CHECKING_TOOLS
            seconds = time.monotonic() - started
            verdict = outcome.result.get("verdict")
            done = {"pass": "Done: checks passed", "fail": "Done: problems found"}.get(
                verdict or "", "Done"
            )
            session.update(activity, status="done", text=f"{done} ({seconds:.0f} s)",
                           verdict=verdict)  # fmt: skip
            if outcome.view is not None:
                views.append(outcome.view)
            for chat_file in outcome.files:
                session.emit({"type": "file", "file": chat_file.public()})
            results.append({"type": "tool_result", "tool_use_id": use["id"],
                            "content": json.dumps(outcome.result, sort_keys=True)})  # fmt: skip
        session.messages.append({"role": "user", "content": results})
    else:
        answer = "I stopped after several steps without finishing. Try asking for one thing."
    if answer and not checked and VERDICT_TALK.search(answer) and views == []:
        prior = any(e.get("type") == "assistant" and e.get("views") for e in session.events)
        if not prior:
            answer = f"{answer}\n\n{UNCHECKED_NOTE}"
    return session.emit({"type": "assistant", "text": answer or "Done.", "views": views})


Runner = Callable[[Callable[[], None]], None]


def thread_runner(job: Callable[[], None]) -> None:
    threading.Thread(target=job, daemon=True, name="syntera-chat-turn").start()


class ChatEngine:
    """Sessions in memory (one Syntera server, a handful of people), turns in the background."""

    def __init__(self, ws: ChatWorkspace, model_factory: Callable[[], ChatModel],
                 runner: Runner = thread_runner):  # fmt: skip
        self.ws, self.model_factory, self.runner = ws, model_factory, runner
        self.sessions: dict[str, ChatSession] = {}
        self.lock = threading.Lock()

    def create(self) -> ChatSession:
        session = ChatSession(id="c" + secrets.token_hex(10))
        with self.lock:
            self.sessions[session.id] = session
        return session

    def get(self, session_id: str) -> ChatSession | None:
        if not SESSION_ID.match(session_id or ""):
            return None
        return self.sessions.get(session_id)

    def send(self, session: ChatSession, text: str, file_ids: list[str]) -> dict[str, Any]:
        files = [self.ws.file(file_id) for file_id in file_ids]
        with session.lock:
            if session.busy:
                raise ChatToolError("Syntera is still working on the last message")
            if session.turns >= MAX_TURNS:
                raise ChatToolError("this conversation is full; start a new chat")
            session.busy = True
            session.turns += 1
        event = session.emit({"type": "user", "text": text,
                              "files": [f.public() for f in files]})  # fmt: skip
        model = self.model_factory()

        def job() -> None:
            try:
                run_turn(session, model, self.ws, text, files)
            except Exception as error:  # report, never leave the session busy
                traceback.print_exc()
                session.emit({"type": "error", "text": f"Something went wrong: {error}"})
            finally:
                session.busy = False

        self.runner(job)
        return event
