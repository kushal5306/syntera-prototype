"""Generated drawing sheets in the web app: view and download (roadmap G3).

``POST /api/sheets`` makes an ISO sheet (C2) from the latest routed run, stores it as
``sheets/<n>/`` with DXF, PDF and (when the ODA File Converter is installed) DWG, and
also files the DXF in the drawings store so the drawings page can display it. Downloads
are limited to those files. Sheets are review drawings; their title block repeats the
verdict from the run's report and is never evidence.
"""

from __future__ import annotations

import contextlib
import re
from pathlib import Path
from threading import Lock

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from syntera.design import DESIGN_FILE

SHEET_ID = re.compile(r"^[0-9]{1,6}$")
FILES = {
    "sheet.dxf": "application/dxf",
    "sheet.pdf": "application/pdf",
    "sheet.dwg": "application/acad",
    "sheet.json": "application/json",
}


def write_pdf(dxf: Path, target: Path, width_mm: float, height_mm: float) -> Path:
    """Plot the sheet to a PDF page of the sheet's own size with ezdxf's drawing add-on."""
    import ezdxf
    import matplotlib

    matplotlib.use("Agg")
    from ezdxf.addons.drawing import matplotlib as plot

    doc = ezdxf.readfile(dxf)
    plot.qsave(doc.modelspace(), str(target), bg="#ffffff",
               size_inches=(width_mm / 25.4, height_mm / 25.4), dpi=150)  # fmt: skip
    return target


def sheets_router(latest: Path, root: Path, drawings_root: Path) -> APIRouter:
    router = APIRouter(prefix="/api/sheets")
    lock = Lock()

    def listing(folder: Path) -> dict:
        number = int(folder.name)
        files = [name for name in FILES if (folder / name).is_file()]
        drawing = (
            (folder / "drawing_id").read_text().strip()
            if (folder / "drawing_id").is_file()
            else None
        )
        return {
            "id": number,
            "drawing_id": drawing,
            "viewer": f"/drawings#{drawing}" if drawing else None,
            "downloads": [{"name": name, "url": f"/api/sheets/{number}/{name}"} for name in files],
        }

    @router.get("/capabilities")
    def capabilities() -> dict:
        from syntera.drawings.export import dwg_writer

        return {"dxf": True, "pdf": True, "dwg": dwg_writer() is not None}

    @router.get("")
    def sheets() -> list[dict]:
        if not root.is_dir():
            return []
        folders = sorted((f for f in root.iterdir() if SHEET_ID.match(f.name)),
                         key=lambda f: int(f.name))  # fmt: skip
        return [listing(folder) for folder in folders]

    @router.post("")
    def create() -> dict:
        from syntera.drawings.convert import DrawingError
        from syntera.drawings.export import dwg_writer, export_dwg
        from syntera.drawings.intake import ingest_drawing
        from syntera.drawings.sheet import SHEET_DXF, sheet_from_run

        if not (latest / DESIGN_FILE).is_file():
            raise HTTPException(status_code=409, detail="route the design first")
        with lock:
            root.mkdir(parents=True, exist_ok=True)
            number = 1 + max((int(f.name) for f in root.iterdir() if SHEET_ID.match(f.name)),
                             default=0)  # fmt: skip
            folder = root / str(number)
            try:
                layout = sheet_from_run(latest, folder)
            except ValueError as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
            choice = layout.choice
            write_pdf(folder / SHEET_DXF, folder / "sheet.pdf", choice.width_mm, choice.height_mm)
            if dwg_writer() is not None:
                # DXF and PDF remain if DWG conversion fails; the listing shows what exists
                with contextlib.suppress(DrawingError):
                    export_dwg(folder / SHEET_DXF, folder / "sheet.dwg")
            summary = ingest_drawing((folder / SHEET_DXF).read_bytes(), f"sheet-{number}.dxf",
                                     drawings_root)  # fmt: skip
            (folder / "drawing_id").write_text(summary.drawing_id, encoding="utf-8")
        return {
            **listing(folder),
            "sheet": choice.sheet,
            "scale": choice.label,
            "assurance_in_title_block": layout.title_block["ASSURANCE (EXACT GEOMETRY CHECKS)"],
        }

    @router.get("/{sheet_id}/{name}")
    def download(sheet_id: str, name: str) -> FileResponse:
        if not SHEET_ID.match(sheet_id) or name not in FILES:
            raise HTTPException(status_code=404, detail="unknown sheet file")
        target = root / sheet_id / name
        if not target.is_file():
            raise HTTPException(status_code=404, detail="not generated")
        return FileResponse(target, media_type=FILES[name], filename=name)

    return router
