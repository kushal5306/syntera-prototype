"""HTTP routes for uploading DWG/DXF drawings and viewing them in the browser."""

from __future__ import annotations

from pathlib import Path
from threading import Lock

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from syntera.drawings.convert import DrawingError, available_converters
from syntera.drawings.intake import (
    DRAWING_ID,
    DXF_FILE,
    MAX_UPLOAD_BYTES,
    SUMMARY_FILE,
    SVG_FILE,
    ingest_drawing,
    list_drawings,
)

DRAWING_FILES = {
    SVG_FILE: "image/svg+xml",
    DXF_FILE: "application/dxf",
    SUMMARY_FILE: "application/json",
}


def drawings_router(root: Path) -> APIRouter:
    """Routes storing drawings under ``root``, one content-addressed folder per file."""
    router = APIRouter(prefix="/api/drawings")
    lock = Lock()

    def ingest(data: bytes, filename: str) -> dict:
        with lock:
            summary = ingest_drawing(data, filename, root)
        return summary.model_dump(mode="json")

    @router.get("/capabilities")
    def capabilities() -> dict:
        converters = available_converters()
        return {
            "dwg": bool(converters),
            "converter": converters[0].name if converters else None,
            "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
        }

    @router.get("")
    def index() -> list[dict]:
        return [
            {
                "drawing_id": item.drawing_id,
                "filename": item.filename,
                "source_format": item.source_format,
            }
            for item in list_drawings(root)
        ]

    @router.post("")
    async def upload(request: Request, filename: str = Query(min_length=1)) -> dict:
        too_large = HTTPException(status_code=413, detail="drawing exceeds the upload limit")
        if int(request.headers.get("content-length") or 0) > MAX_UPLOAD_BYTES:
            raise too_large
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                raise too_large
            chunks.append(chunk)
        data = b"".join(chunks)
        try:
            return await run_in_threadpool(ingest, data, filename)
        except DrawingError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @router.get("/{drawing_id}/{name}")
    def drawing_file(drawing_id: str, name: str) -> FileResponse:
        if not DRAWING_ID.match(drawing_id) or name not in DRAWING_FILES:
            raise HTTPException(status_code=404, detail="unknown drawing output")
        target = root / drawing_id / name
        if not target.is_file():
            raise HTTPException(status_code=404, detail="drawing has not been processed")
        return FileResponse(target, media_type=DRAWING_FILES[name])

    return router
