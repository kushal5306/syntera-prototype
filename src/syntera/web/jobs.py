"""``/api/jobs``: run routing, FEA and optimisation as background jobs (roadmap I1)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from syntera.design import DESIGN_FILE, DesignDocument, load_design, load_design_input
from syntera.jobs import InProcessQueue, JobError, JobQueue
from syntera.web.optimise import run_cache


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1, max_length=40)
    payload: dict[str, Any] = Field(default_factory=dict)


def current_design(config_path: Path, design_root: Path) -> DesignDocument:
    """The latest stored design version (G2/G4), else the configured design."""
    index = design_root / "versions.json"
    if index.is_file():
        versions = json.loads(index.read_text(encoding="utf-8"))
        if versions:
            return load_design(design_root / f"v{versions[-1]['number']:04d}.json")
    return load_design_input(config_path)


def make_handlers(config_path: Path, output_root: Path) -> dict:
    def route_design_job(payload: dict, folder: Path) -> dict:
        run = run_cache(output_root).route_design(
            current_design(config_path, output_root / "design"), folder
        )
        return {
            "cached": run.hit,
            "overall_pass": run.report.overall_pass,
            "lines": {line.line: line.overall_pass for line in run.report.lines},
            "failure_reasons": run.report.failure_reasons,
            "source": "exact geometry checks (route-design)",
            "output": folder.name,
        }

    def optimise_job(payload: dict, folder: Path) -> dict:
        from syntera.optimise.optimiser import optimise
        from syntera.optimise.problem import OptimisationProblem, check_problem
        from syntera.optimise.report import build_report
        from syntera.web.optimise import Study

        study = Study.model_validate(payload)
        problem = OptimisationProblem(design=str(config_path), **study.model_dump())
        design = load_design_input(config_path)
        check_problem(problem, design)
        result = optimise(problem, design, folder, run_cache(output_root))
        return build_report(problem, result).model_dump(mode="json")

    def fea_job(payload: dict, folder: Path) -> dict:
        from syntera.analysis.calculix import MANIFEST_FILE, input_sha256
        from syntera.analysis.fields import solve_decks
        from syntera.analysis.results import evaluate_acceptance
        from syntera.design import run_config
        from syntera.schemas import FeaManifest

        latest = output_root / "latest"
        manifest_path = latest / "fea" / MANIFEST_FILE
        if not manifest_path.is_file() or not (latest / DESIGN_FILE).is_file():
            raise ValueError("route the design in the workbench first; no FEA decks exist")
        manifest = FeaManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
        outcome = solve_decks(latest / "fea", manifest)
        if not outcome.solved:
            raise RuntimeError(outcome.diagnostic or "CalculiX did not solve the decks")
        design = load_design(latest / DESIGN_FILE)
        route = next(r for r in design.routes if r.line == design.lines[0].id)
        digest = input_sha256(run_config(design, design.lines[0].id), route.points)
        report = evaluate_acceptance(manifest, latest / "fea", digest)
        return {
            "overall_pass": report.overall_pass,
            "failure_reasons": report.failure_reasons,
            "source": "FEA acceptance thresholds on solved CalculiX results",
        }

    return {"route-design": route_design_job, "optimise": optimise_job, "fea": fea_job}


def jobs_router(queue: JobQueue) -> APIRouter:
    router = APIRouter(prefix="/api/jobs")

    @router.post("", status_code=202)
    def submit(request: JobRequest) -> dict:
        try:
            record = queue.submit(request.kind, request.payload)
        except JobError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {**record.model_dump(mode="json"), "status_url": f"/api/jobs/{record.id}"}

    @router.get("")
    def listing() -> list[dict]:
        return [record.model_dump(mode="json", exclude={"result"}) for record in queue.list()]

    @router.get("/{job_id}")
    def status(job_id: str) -> dict:
        try:
            return queue.get(job_id).model_dump(mode="json")
        except KeyError as error:
            raise HTTPException(status_code=404, detail="no such job") from error

    return router


def in_process_queue(config_path: Path, output_root: Path) -> InProcessQueue:
    return InProcessQueue(output_root / "jobs", make_handlers(config_path, output_root))
