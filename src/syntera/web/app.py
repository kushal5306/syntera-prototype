"""FastAPI application exposing CAD geometry and deterministic routing."""

from __future__ import annotations

import shutil
from pathlib import Path
from threading import Lock
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from syntera.analysis.calculix import input_sha256
from syntera.analysis.fields import parse_case_fields, parse_mesh_inp, solve_decks
from syntera.analysis.results import evaluate_acceptance
from syntera.config import load_config
from syntera.geometry.cad import assembly_shapes
from syntera.geometry.step_import import StepImportError
from syntera.pipeline import PipelineResult, execute_pipeline
from syntera.reporting.timelog import (
    TIME_LOG_FILE,
    append_event,
    kpi1_from_files,
    make_event,
    pair_intervals,
    read_log,
    task_hours,
)
from syntera.schemas import DemoConfig, Label, StepObstacle, TimeActor
from syntera.web.audit import audit_router
from syntera.web.drawings import drawings_router
from syntera.web.mesh import scene_meshes
from syntera.web.pid import pid_router

DOWNLOADS = {
    "assembly.step",
    "routed_assembly.step",
    "route.json",
    "assurance_report.json",
    "route_preview.png",
    "import_report.json",
    "metrics.json",
    "layout.dxf",
    "rationale.json",
}


def _step_paths(config: DemoConfig) -> set[str]:
    return {
        str(Path(obstacle.path).resolve())
        for obstacle in config.obstacles
        if isinstance(obstacle, StepObstacle)
    }


def fea_payload(config: DemoConfig, result: PipelineResult, output: Path) -> dict | None:
    """Solve the generated decks and return display fields plus the deterministic verdict."""
    if result.fea is None:
        return None
    manifest = result.fea
    fea_dir = output / "fea"
    outcome = solve_decks(fea_dir, manifest)
    payload: dict = {
        "manifest": manifest.model_dump(mode="json"),
        "solved": outcome.solved,
        "diagnostic": outcome.diagnostic,
    }
    if not outcome.solved:
        return payload
    model = parse_mesh_inp((fea_dir / manifest.mesh_file).read_text(encoding="utf-8"))
    cases = [
        parse_case_fields(
            deck.name, (fea_dir / deck.result_file).read_text(encoding="utf-8"), model
        )
        for deck in manifest.load_cases
    ]
    acceptance = evaluate_acceptance(manifest, fea_dir, input_sha256(config, result.route.points))
    payload.update(
        nodes=[[round(value, 3) for value in node] for node in model.nodes],
        elements=model.elements,
        cases=[
            {"name": case.name, "displacement": case.displacement, "stress": case.element_von_mises}
            for case in cases
        ],
        acceptance=acceptance.model_dump(mode="json"),
    )
    return payload


class TimeMark(BaseModel):
    """An engineer's phase start or stop; the server stamps the time, never the browser."""

    task: Label
    phase: Label
    event: Literal["start", "stop"]
    note: str | None = Field(default=None, max_length=500)


def create_app(
    config_path: Path, output_root: Path, manual_baseline: Path | None = None
) -> FastAPI:
    """Create an application bound to one initial config and controlled output root."""
    static = Path(__file__).with_name("static")
    output = output_root / "latest"
    time_log = output_root / TIME_LOG_FILE
    lock = Lock()
    time_lock = Lock()
    app = FastAPI(title="Syntera interactive prototype", version="0.2.0")
    app.mount("/assets", StaticFiles(directory=static), name="assets")
    app.include_router(drawings_router(output_root / "drawings"))
    app.include_router(pid_router(output_root / "pid"))
    app.include_router(audit_router(output_root / "audit"))

    def checked(config: DemoConfig) -> DemoConfig:
        """Only STEP files named by the server-side config may be opened from a request."""
        unknown = _step_paths(config) - _step_paths(load_config(config_path))
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=f"STEP files not listed in the server configuration: {sorted(unknown)}",
            )
        return config

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(static / "index.html")

    @app.get("/drawings", include_in_schema=False)
    def drawings_page() -> FileResponse:
        return FileResponse(static / "drawings.html")

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "engine": "deterministic"}

    @app.get("/api/capabilities")
    def capabilities() -> dict[str, bool]:
        return {"calculix": shutil.which("ccx") is not None}

    @app.get("/api/config")
    def get_config() -> dict:
        return load_config(config_path).model_dump(mode="json")

    @app.post("/api/preview")
    def preview(config: DemoConfig) -> dict:
        try:
            assembly = assembly_shapes(checked(config))
        except StepImportError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {
            "workspace": config.workspace.model_dump(mode="json"),
            "meshes": scene_meshes(config, assembly),
        }

    def log_time(
        task: str, phase: str, event: str, actor: TimeActor = "engineer", note: str | None = None
    ) -> None:
        with time_lock:
            append_event(time_log, make_event(task, phase, event, actor=actor, note=note))

    @app.post("/api/route")
    def route(config: DemoConfig, task: Label | None = None) -> dict:
        with lock:
            if task:
                log_time(task, "generate", "start", actor="syntera")
            try:
                result = execute_pipeline(checked(config), output)
            except StepImportError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
            finally:
                if task:
                    log_time(task, "generate", "stop", actor="syntera")
            return {
                "config": config.model_dump(mode="json"),
                "workspace": config.workspace.model_dump(mode="json"),
                "meshes": scene_meshes(config, result.assembly, result.tube),
                "fea": fea_payload(config, result, output),
                "route": result.route.model_dump(mode="json"),
                "assurance": result.report.model_dump(mode="json"),
                "metrics": result.metrics.model_dump(mode="json") if result.metrics else None,
                "downloads": [
                    {"name": name, "url": f"/api/download/{name}"}
                    for name in sorted(DOWNLOADS)
                    if (output / name).is_file()
                ],
            }

    @app.post("/api/time")
    def mark_time(mark: TimeMark) -> dict:
        log_time(mark.task, mark.phase, mark.event, note=mark.note)
        return time_summary()

    @app.get("/api/time")
    def time_summary() -> dict:
        with time_lock:
            log = read_log(time_log)
        _, errors, still_open = pair_intervals(log.events)
        return {
            "task_hours": task_hours(log.events),
            "running": [event.model_dump(mode="json") for event in still_open],
            "errors": log.errors + errors,
            "recent": [event.model_dump(mode="json") for event in log.events[-20:]],
        }

    @app.get("/api/kpi1")
    def kpi1() -> dict:
        if manual_baseline is None or not manual_baseline.is_file():
            raise HTTPException(status_code=404, detail="no manual baseline is configured")
        with time_lock:
            report = kpi1_from_files(time_log, manual_baseline)
        return report.model_dump(mode="json")

    @app.get("/api/download/{filename}")
    def download(filename: str) -> FileResponse:
        if filename not in DOWNLOADS:
            raise HTTPException(status_code=404, detail="unknown output")
        target = output / filename
        if not target.is_file():
            raise HTTPException(status_code=404, detail="output has not been generated")
        return FileResponse(target, filename=filename)

    return app
