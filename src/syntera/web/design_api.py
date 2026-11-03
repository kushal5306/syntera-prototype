"""Design editing and history over HTTP (roadmap G2 and G4).

``/api/design`` serves the current design document's display data and lets the user
edit it through typed change sets: ``preview`` shows the before/after diff without
applying anything, ``apply`` applies the user's change set, stores a new version and can
regenerate (route and verify) it. Every version is kept on disk, so any two can be
compared. Change sets here cannot write routes or rationale: routes come from the
pipeline and rationale records from the rules or the edit itself.
"""

from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from syntera.changes import ChangeSet, apply_change_set, diff_designs
from syntera.design import (
    DesignDocument,
    DesignError,
    design_sha256,
    load_design,
    load_design_input,
    save_design,
)

EDITABLE_SUFFIXES = (
    "tube.outer_diameter",
    "tube.minimum_bend_radius",
    "tube.minimum_clearance",
)
EDITABLE_PARAMETERS = (
    "parameters.voxel_resolution",
    "parameters.routing_cost_weights.bend",
    "parameters.routing_cost_weights.obstacle_proximity",
)
OUTPUT_OPS = frozenset({"set_routes", "set_rationale"})
INDEX_FILE = "versions.json"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VersionInfo(StrictModel):
    number: int
    sha256: str
    description: str
    parent: int | None


class EditRequest(StrictModel):
    changes: ChangeSet
    regenerate: bool = False


class PreviewRequest(StrictModel):
    changes: ChangeSet


class DesignStore:
    """Versions of one design document on disk: ``v0001.json``, ``v0002.json``…"""

    def __init__(self, root: Path, initial: DesignDocument):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        index = root / INDEX_FILE
        if index.is_file():
            raw = json.loads(index.read_text(encoding="utf-8"))
            self.versions = [VersionInfo.model_validate(item) for item in raw]
        else:
            self.versions = []
            self.add(initial, "initial design", None)

    def path(self, number: int) -> Path:
        return self.root / f"v{number:04d}.json"

    def add(self, design: DesignDocument, description: str, parent: int | None) -> VersionInfo:
        info = VersionInfo(number=len(self.versions) + 1, sha256=design_sha256(design),
                           description=description, parent=parent)  # fmt: skip
        save_design(design, self.path(info.number))
        self.versions.append(info)
        (self.root / INDEX_FILE).write_text(
            json.dumps([v.model_dump(mode="json") for v in self.versions], indent=2) + "\n",
            encoding="utf-8",
        )
        return info

    def get(self, number: int) -> DesignDocument:
        if not 1 <= number <= len(self.versions):
            raise KeyError(number)
        return load_design(self.path(number))

    @property
    def current(self) -> tuple[VersionInfo, DesignDocument]:
        info = self.versions[-1]
        return info, self.get(info.number)


def editable(path: str, design: DesignDocument) -> bool:
    if path in EDITABLE_PARAMETERS:
        return True
    return any(path == f"lines.{line.id}.{suffix}" for line in design.lines
               for suffix in EDITABLE_SUFFIXES)  # fmt: skip


def design_view(info: VersionInfo, design: DesignDocument) -> dict[str, Any]:
    """Display data for the browser: values, their sources and which ones can be edited."""
    return {
        "version": info.number,
        "sha256": info.sha256,
        "title": design.title,
        "synthetic_data": design.synthetic_data,
        "lines": [
            {"id": line.id, "from": line.from_nozzle, "to": line.to_nozzle,
             "tube": line.tube.model_dump(mode="json"),
             "routed": any(route.line == line.id for route in design.routes)}
            for line in design.lines
        ],
        "values": [
            {"path": record.path, "value": record.value, "unit": record.unit,
             "source": record.source, "text": record.text(),
             "editable": editable(record.path, design)}
            for record in design.rationale
        ],
    }  # fmt: skip


def _check(changes: ChangeSet) -> None:
    for number, edit in enumerate(changes.edits, start=1):
        if edit.op in OUTPUT_OPS:
            raise HTTPException(
                status_code=422, detail=f"edit {number}: {edit.op} is not an editable input"
            )


def design_router(config_path: Path, root: Path) -> APIRouter:
    router = APIRouter(prefix="/api/design")
    lock = Lock()
    holder: dict[str, DesignStore] = {}

    def store() -> DesignStore:
        if "store" not in holder:
            holder["store"] = DesignStore(root, load_design_input(config_path))
        return holder["store"]

    @router.get("")
    def current() -> dict:
        with lock:
            info, design = store().current
        return design_view(info, design)

    @router.post("/preview")
    def preview(body: PreviewRequest) -> dict:
        _check(body.changes)
        with lock:
            info, design = store().current
        try:
            changed, _ = apply_change_set(design, body.changes)
        except DesignError as error:
            return {"valid": False, "error": str(error), "version": info.number}
        diff = diff_designs(design, changed)
        return {"valid": True, "version": info.number, "diff": diff.model_dump(mode="json")}

    @router.post("/apply")
    def apply(body: EditRequest) -> dict:
        _check(body.changes)
        with lock:
            designs = store()
            info, design = designs.current
            try:
                changed, revert = apply_change_set(design, body.changes)
            except DesignError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
            verdicts = None
            if body.regenerate:
                from syntera.multiline import route_design

                run = route_design(changed, root / "runs" / f"v{len(designs.versions) + 1:04d}")
                changed = run.design
                verdicts = {
                    "overall_pass": run.report.overall_pass,
                    "lines": {line.line: line.overall_pass for line in run.report.lines},
                    "failure_reasons": run.report.failure_reasons,
                    "source": "exact geometry checks (route-design)",
                }
            new = designs.add(changed, body.changes.description, info.number)
        return {
            "version": new.number,
            "sha256": new.sha256,
            "diff": diff_designs(design, changed).model_dump(mode="json"),
            "revert": revert.model_dump(mode="json"),
            "verdicts": verdicts,
        }

    @router.get("/versions")
    def versions() -> list[dict]:
        with lock:
            return [info.model_dump(mode="json") for info in store().versions]

    @router.get("/versions/{number}")
    def version(number: int) -> dict:
        with lock:
            designs = store()
            try:
                design = designs.get(number)
            except KeyError as error:
                raise HTTPException(status_code=404, detail="no such version") from error
            return design_view(designs.versions[number - 1], design)

    @router.get("/compare")
    def compare(a: int = Query(ge=1), b: int = Query(ge=1)) -> dict:
        with lock:
            designs = store()
            try:
                before, after = designs.get(a), designs.get(b)
            except KeyError as error:
                raise HTTPException(status_code=404, detail="no such version") from error
        return {"a": a, "b": b, "diff": diff_designs(before, after).model_dump(mode="json")}

    return router
