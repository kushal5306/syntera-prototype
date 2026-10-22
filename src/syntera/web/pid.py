"""HTTP routes turning an uploaded DEXPI P&ID into a proposed 3D plant layout."""

from __future__ import annotations

import re
from pathlib import Path
from threading import Lock

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from syntera.pid.build import CONFIG_FILE, PLANT_STEP, REPORT_FILE, PlantResult, build_plant
from syntera.pid.dexpi import MAX_XML_BYTES, PidError, read_dexpi
from syntera.rationale import RATIONALE_FILE
from syntera.web.mesh import shape_mesh

PLANT_ID = re.compile(r"^[0-9a-f]{16}$")
ROUTE_FOLDER = re.compile(r"^route-[1-9][0-9]{0,2}$")
PLANT_FILES = {
    REPORT_FILE: "application/json",
    PLANT_STEP: "application/step",
    RATIONALE_FILE: "application/json",
}
ROUTE_FILES = {
    CONFIG_FILE: "application/json",
    "routed_assembly.step": "application/step",
    "assurance_report.json": "application/json",
    "layout.dxf": "application/dxf",
    RATIONALE_FILE: "application/json",
}
EQUIPMENT_COLOUR = (0.88, 0.34, 0.25)
PIPE_COLOUR = (0.08, 0.72, 0.82)


def plant_meshes(result: PlantResult) -> list[dict]:
    tolerance = max(2.0, result.report.voxel_resolution_mm / 8)
    meshes = []
    for i, (item, shape) in enumerate(zip(result.report.equipment, result.equipment, strict=True)):
        properties = item.model_dump(mode="json")
        meshes.append(
            shape_mesh(f"equipment-{i}", item.tag, "obstacle", shape, EQUIPMENT_COLOUR,
                       properties, tolerance)
        )  # fmt: skip
    for i, geometry in enumerate(result.routes):
        if geometry.tube is None:
            continue
        properties = geometry.route.model_dump(mode="json")
        for j, shape in enumerate([geometry.tube, *geometry.stubs]):
            meshes.append(
                shape_mesh(f"route-{i + 1}-{j}", f"line {geometry.route.line}", "route",
                           shape, PIPE_COLOUR, properties, tolerance)
            )  # fmt: skip
    return meshes


def pid_router(root: Path) -> APIRouter:
    """Routes storing each P&ID's proposed layout under ``root/<id>``."""
    router = APIRouter(prefix="/api/pid")
    lock = Lock()

    def build(data: bytes, filename: str) -> dict:
        document = read_dexpi(data, filename)
        folder = root / document.source_sha256[:16]
        with lock:
            result = build_plant(document, folder / "plant")
            (folder / "source.xml").write_bytes(data)
        return {
            "plant_id": document.source_sha256[:16],
            "report": result.report.model_dump(mode="json"),
            "connections": [c.model_dump(mode="json") for c in document.connections],
            "meshes": plant_meshes(result),
        }

    @router.post("")
    async def upload(request: Request, filename: str = Query(min_length=1)) -> dict:
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_XML_BYTES:
                raise HTTPException(status_code=413, detail="P&ID exceeds the upload limit")
            chunks.append(chunk)
        try:
            return await run_in_threadpool(build, b"".join(chunks), filename)
        except (PidError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @router.get("/{plant_id}/{name}")
    def plant_file(plant_id: str, name: str) -> FileResponse:
        if not PLANT_ID.match(plant_id) or name not in PLANT_FILES:
            raise HTTPException(status_code=404, detail="unknown plant output")
        return _existing(root / plant_id / "plant" / name, PLANT_FILES[name], name)

    @router.get("/{plant_id}/{route}/{name}")
    def route_file(plant_id: str, route: str, name: str) -> FileResponse:
        if not PLANT_ID.match(plant_id) or not ROUTE_FOLDER.match(route) or name not in ROUTE_FILES:
            raise HTTPException(status_code=404, detail="unknown plant output")
        target = root / plant_id / "plant" / route / name
        return _existing(target, ROUTE_FILES[name], f"{route}-{name}")

    return router


def _existing(target: Path, media_type: str, filename: str) -> FileResponse:
    if not target.is_file():
        raise HTTPException(status_code=404, detail="the plant layout has not been generated")
    return FileResponse(target, media_type=media_type, filename=filename)
