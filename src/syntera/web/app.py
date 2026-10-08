"""FastAPI application exposing CAD geometry and deterministic routing."""

from __future__ import annotations

import shutil
from pathlib import Path
from threading import Lock

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from syntera.analysis.calculix import input_sha256
from syntera.analysis.fields import parse_case_fields, parse_mesh_inp, solve_decks
from syntera.analysis.results import evaluate_acceptance
from syntera.config import load_config
from syntera.geometry.cad import assembly_shapes
from syntera.geometry.step_import import StepImportError
from syntera.pipeline import PipelineResult, execute_pipeline
from syntera.schemas import DemoConfig, StepObstacle
from syntera.web.mesh import scene_meshes

DOWNLOADS = {
    "assembly.step",
    "routed_assembly.step",
    "route.json",
    "assurance_report.json",
    "route_preview.png",
    "import_report.json",
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


def create_app(config_path: Path, output_root: Path) -> FastAPI:
    """Create an application bound to one initial config and controlled output root."""
    static = Path(__file__).with_name("static")
    output = output_root / "latest"
    lock = Lock()
    app = FastAPI(title="Syntera interactive prototype", version="0.2.0")
    app.mount("/assets", StaticFiles(directory=static), name="assets")

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

    @app.post("/api/route")
    def route(config: DemoConfig) -> dict:
        with lock:
            try:
                result = execute_pipeline(checked(config), output)
            except StepImportError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
            return {
                "config": config.model_dump(mode="json"),
                "workspace": config.workspace.model_dump(mode="json"),
                "meshes": scene_meshes(config, result.assembly, result.tube),
                "fea": fea_payload(config, result, output),
                "route": result.route.model_dump(mode="json"),
                "assurance": result.report.model_dump(mode="json"),
                "downloads": [
                    {"name": name, "url": f"/api/download/{name}"}
                    for name in sorted(DOWNLOADS)
                    if (output / name).is_file()
                ],
            }

    @app.get("/api/download/{filename}")
    def download(filename: str) -> FileResponse:
        if filename not in DOWNLOADS:
            raise HTTPException(status_code=404, detail="unknown output")
        target = output / filename
        if not target.is_file():
            raise HTTPException(status_code=404, detail="output has not been generated")
        return FileResponse(target, filename=filename)

    return app
