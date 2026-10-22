"""HTTP routes auditing an uploaded STEP assembly as a whole (clashes, clearances, joints)."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from threading import Lock

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from syntera.geometry.step_import import StepImportError
from syntera.verification.step_audit import StepAuditResult, audit_step
from syntera.web.mesh import shape_mesh

MAX_STEP_BYTES = 100 * 1024 * 1024
AUDIT_ID = re.compile(r"^[0-9a-f]{16}$")
AUDIT_FILE = "audit.json"
EQUIPMENT_COLOUR = (0.88, 0.34, 0.25)
PIPE_COLOUR = (0.08, 0.72, 0.82)
FLAGGED_COLOUR = (0.95, 0.15, 0.2)


def audit_payload(audit_id: str, filename: str, result: StepAuditResult) -> dict:
    return {
        "audit_id": audit_id,
        "filename": filename,
        "pipes": result.pipes,
        "equipment": result.equipment,
        "sizes_mm": list(result.sizes_mm),
        "audit": result.audit.model_dump(mode="json"),
    }


def audit_meshes(result: StepAuditResult) -> list[dict]:
    """Solids named in a failing finding are shown red; the rest by role."""
    flagged = {
        name
        for rule in result.audit.rules
        if rule.status == "fail"
        for finding in rule.findings
        for name in finding.parts
    }
    boxes = [part.shape.BoundingBox() for part in result.parts]
    span = max((max(b.xlen, b.ylen, b.zlen) for b in boxes), default=1000.0)
    tolerance = max(0.5, span / 2000)
    meshes = []
    for index, part in enumerate(result.parts):
        colour = (
            FLAGGED_COLOUR
            if part.name in flagged
            else (PIPE_COLOUR if part.role == "pipe" else EQUIPMENT_COLOUR)
        )
        kind = "route" if part.role == "pipe" else "obstacle"
        properties = {"role": part.role, "outer_diameter_mm": part.outer_diameter}
        meshes.append(
            shape_mesh(f"solid-{index}", part.name, kind, part.shape, colour, properties, tolerance)
        )
    return meshes


def audit_router(root: Path) -> APIRouter:
    """Routes storing each audited STEP file and its report under ``root/<id>``."""
    router = APIRouter(prefix="/api/audit")
    lock = Lock()

    def run(data: bytes, filename: str) -> dict:
        audit_id = hashlib.sha256(data).hexdigest()[:16]
        folder = root / audit_id
        with lock:
            folder.mkdir(parents=True, exist_ok=True)
            source = folder / "source.step"
            source.write_bytes(data)
            result = audit_step(source)
            payload = audit_payload(audit_id, filename, result)
            (folder / AUDIT_FILE).write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        return {**payload, "meshes": audit_meshes(result)}

    @router.post("")
    async def upload(request: Request, filename: str = Query(min_length=1)) -> dict:
        if not re.search(r"\.(step|stp)$", filename, re.IGNORECASE):
            raise HTTPException(status_code=422, detail="upload a STEP file (.step or .stp)")
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_STEP_BYTES:
                raise HTTPException(status_code=413, detail="STEP file exceeds the upload limit")
            chunks.append(chunk)
        try:
            return await run_in_threadpool(run, b"".join(chunks), filename)
        except (StepImportError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @router.get("/{audit_id}/audit.json")
    def report(audit_id: str) -> FileResponse:
        target = root / audit_id / AUDIT_FILE
        if not AUDIT_ID.match(audit_id) or not target.is_file():
            raise HTTPException(status_code=404, detail="unknown audit")
        return FileResponse(target, media_type="application/json", filename=AUDIT_FILE)

    return router
