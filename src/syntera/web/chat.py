"""``/api/chat``: the plain-language front door to Syntera.

* ``GET /api/chat/status``: which model answers (Claude, or basic mode without a key).
* ``POST /api/chat/files?filename=``: upload a drawing, P&ID or STEP model (raw body).
* ``POST /api/chat/sessions``: start a conversation.
* ``POST /api/chat/sessions/{id}/messages``: send a message; the turn runs in the background.
* ``GET /api/chat/sessions/{id}?since=n``: events after ``n`` and whether Syntera is busy.
* ``GET /api/chat/views/{id}``: the 3D meshes and result card of one result.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from syntera.chat.models import ChatModel, chat_model_from_env
from syntera.chat.session import ChatEngine, ChatSession, Runner, thread_runner
from syntera.chat.tools import MAX_CHAT_UPLOAD_BYTES, VIEW_ID, ChatToolError, ChatWorkspace

ChatModelFactory = Callable[[], ChatModel]


class ChatMessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(default="", max_length=4000)
    file_ids: list[str] = Field(default_factory=list, max_length=5)


def chat_router(
    output_root: Path,
    examples: Path | None,
    model_factory: ChatModelFactory = chat_model_from_env,
    runner: Runner = thread_runner,
) -> APIRouter:
    router = APIRouter(prefix="/api/chat")
    workspace = ChatWorkspace(output_root=output_root, examples=examples)
    engine = ChatEngine(workspace, model_factory, runner)

    def session_or_404(session_id: str) -> ChatSession:
        session = engine.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="unknown conversation; start a new chat")
        return session

    @router.get("/status")
    def status() -> dict:
        model = model_factory()
        return {
            "model": model.name,
            "label": model.label,
            "ai": model.name != "basic",
            "max_upload_mb": MAX_CHAT_UPLOAD_BYTES // (1024 * 1024),
        }

    @router.post("/files")
    async def upload(request: Request, filename: str = Query(min_length=1, max_length=200)):
        too_large = HTTPException(status_code=413, detail="the file is larger than 100 MB")
        if int(request.headers.get("content-length") or 0) > MAX_CHAT_UPLOAD_BYTES:
            raise too_large
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_CHAT_UPLOAD_BYTES:
                raise too_large
            chunks.append(chunk)
        try:
            stored = await run_in_threadpool(workspace.store_file, b"".join(chunks), filename)
        except ChatToolError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return stored.public()

    @router.post("/sessions")
    def create() -> dict:
        return engine.create().snapshot()

    @router.get("/sessions/{session_id}")
    def read(session_id: str, since: int = Query(default=0, ge=0)) -> dict:
        return session_or_404(session_id).snapshot(since)

    @router.post("/sessions/{session_id}/messages", status_code=202)
    def send(session_id: str, body: ChatMessageIn) -> dict:
        session = session_or_404(session_id)
        if not body.text.strip() and not body.file_ids:
            raise HTTPException(status_code=422, detail="write a message or attach a file")
        try:
            event = engine.send(session, body.text, body.file_ids)
        except ChatToolError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"accepted": event, "busy": session.busy}

    @router.get("/views/{view_id}")
    def view(view_id: str) -> FileResponse:
        path = workspace.views_root / f"{view_id}.json"
        if not VIEW_ID.match(view_id) or not path.is_file():
            raise HTTPException(status_code=404, detail="unknown result")
        return FileResponse(path, media_type="application/json")

    return router
