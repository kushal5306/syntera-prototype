"""HTTP routes for uploading DWG/DXF drawings and viewing them in the browser."""

from __future__ import annotations

import re
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
    load_summary,
    refresh_drawing,
)
from syntera.drawings.models import AssemblyOptions
from syntera.drawings.piping_check import (
    ASSEMBLY_DIR,
    ASSEMBLY_FILES,
    LINE_FILES,
    OTHER_LINE,
    PipingCheckResult,
    check_drawing,
)
from syntera.drawings.reader import load_dxf
from syntera.web.mesh import scene_meshes, shape_mesh

DRAWING_FILES = {
    SVG_FILE: "image/svg+xml",
    DXF_FILE: "application/dxf",
    SUMMARY_FILE: "application/json",
}
LINE_FOLDER = re.compile(r"^line-[1-9][0-9]?$")
DRAWN_TUBE_COLOUR = (0.93, 0.62, 0.16)
# Round vessels would otherwise share the pipes' orange; columns and steel are grey.
EQUIPMENT_COLOUR = (0.88, 0.34, 0.25)
STRUCTURE_COLOUR = (0.62, 0.65, 0.70)


def assembly_meshes(result: PipingCheckResult) -> list[dict]:
    """Equipment once, then each line's nozzles, drawn tube and Syntera's tube."""
    meshes: list[dict] = []
    for index, line in enumerate(result.lines):
        prefix = f"line-{index + 1}"
        name = result.report.lines[index].name
        for mesh in scene_meshes(line.config, line.assembly, line.optimised_tube):
            # other lines' legs are routing obstacles; they are drawn as their own tubes
            if mesh["kind"] == "obstacle" and (index or mesh["name"].startswith(OTHER_LINE)):
                continue
            if mesh["kind"] == "obstacle":
                structure = mesh["name"] in result.structures
                mesh["color"] = STRUCTURE_COLOUR if structure else EQUIPMENT_COLOUR
            if mesh["kind"] != "obstacle":
                mesh["id"] = f"{prefix}-{mesh['id']}"
                mesh["name"] = f"{name} {mesh['name'].lower()}"
                mesh["line"] = index
            if mesh["kind"] == "route":
                mesh["name"] = f"{name} Syntera route"
            meshes.append(mesh)
        if line.drawn_tube is not None:
            mesh = shape_mesh(
                f"{prefix}-drawn-tube",
                f"{name} as drawn",
                "drawn",
                line.drawn_tube,
                DRAWN_TUBE_COLOUR,
                {"outer_diameter_mm": line.config.tube.outer_diameter},
                max(0.8, line.config.voxel_resolution / 8.0),
            )
            mesh["line"] = index
            meshes.append(mesh)
        for number, stub in enumerate(line.stubs, start=1):
            mesh = shape_mesh(
                f"{prefix}-nozzle-{number}",
                f"{name} nozzle",
                "drawn",
                stub,
                DRAWN_TUBE_COLOUR,
                {"outer_diameter_mm": line.config.tube.outer_diameter},
                max(0.8, line.config.voxel_resolution / 8.0),
            )
            mesh["line"] = index
            meshes.append(mesh)
    return meshes


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

    def run_check(drawing_id: str, options: AssemblyOptions) -> dict:
        folder = root / drawing_id
        with lock:
            refresh_drawing(root, drawing_id)
            summary = load_summary(folder)
            doc, _, _ = load_dxf(folder / DXF_FILE)
            result = check_drawing(doc, summary, folder, options)
        return {
            "report": result.report.model_dump(mode="json"),
            "meshes": assembly_meshes(result),
        }

    @router.post("/{drawing_id}/assembly")
    async def assembly(drawing_id: str, options: AssemblyOptions) -> dict:
        if not DRAWING_ID.match(drawing_id) or not (root / drawing_id / SUMMARY_FILE).is_file():
            raise HTTPException(status_code=404, detail="unknown drawing")
        try:
            return await run_in_threadpool(run_check, drawing_id, options)
        except (DrawingError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @router.get("/{drawing_id}/assembly/{name}")
    def assembly_report(drawing_id: str, name: str) -> FileResponse:
        if not DRAWING_ID.match(drawing_id) or name not in ASSEMBLY_FILES:
            raise HTTPException(status_code=404, detail="unknown assembly output")
        return _existing(root / drawing_id / ASSEMBLY_DIR / name, ASSEMBLY_FILES[name],
                         name if name.endswith(".step") else None)  # fmt: skip

    @router.get("/{drawing_id}/assembly/{line}/{name}")
    def assembly_file(drawing_id: str, line: str, name: str) -> FileResponse:
        if (
            not DRAWING_ID.match(drawing_id)
            or not LINE_FOLDER.match(line)
            or name not in LINE_FILES
        ):
            raise HTTPException(status_code=404, detail="unknown assembly output")
        target = root / drawing_id / ASSEMBLY_DIR / line / name
        return _existing(target, LINE_FILES[name], f"{line}-{name}")

    @router.get("/{drawing_id}/{name}")
    def drawing_file(drawing_id: str, name: str) -> FileResponse:
        if not DRAWING_ID.match(drawing_id) or name not in DRAWING_FILES:
            raise HTTPException(status_code=404, detail="unknown drawing output")
        with lock:
            try:
                refresh_drawing(root, drawing_id)
            except DrawingError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
        target = root / drawing_id / name
        if not target.is_file():
            raise HTTPException(status_code=404, detail="drawing has not been processed")
        return FileResponse(target, media_type=DRAWING_FILES[name])

    return router


def _existing(target: Path, media_type: str, filename: str | None = None) -> FileResponse:
    if not target.is_file():
        raise HTTPException(status_code=404, detail="the 3D assembly has not been generated")
    return FileResponse(target, media_type=media_type, filename=filename)
