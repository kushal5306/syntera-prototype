"""Optimisation study and Pareto view in the web app (roadmap H3).

``POST /api/optimise`` runs a (small) optimisation over the server's configured design:
every candidate is routed and verified, and the report with the Pareto set, trade-offs
and sensitivity comes back as display data. The problem may not name another design.
"""

from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from syntera.design import load_design_input
from syntera.optimise.problem import (
    Constraints,
    CostModel,
    ObjectiveName,
    OptimisationProblem,
    ProblemError,
    Variable,
    check_problem,
)

WEB_BUDGET = 20


class Study(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="Web optimisation study", min_length=1, max_length=200)
    variables: list[Variable] = Field(min_length=1)
    objectives: list[ObjectiveName] = Field(min_length=1)
    constraints: Constraints = Field(default_factory=Constraints)
    cost: CostModel = Field(default_factory=CostModel)
    strategy: Literal["grid", "random", "bayes"] = "grid"
    seed: int = 0
    budget: Annotated[int, Field(ge=1, le=WEB_BUDGET)] = 9


def optimise_router(config_path: Path, root: Path) -> APIRouter:
    router = APIRouter(prefix="/api/optimise")
    lock = Lock()

    @router.get("")
    def latest() -> dict:
        report = root / "latest" / "optimisation_report.json"
        if not report.is_file():
            raise HTTPException(status_code=404, detail="no optimisation has been run")
        return json.loads(report.read_text(encoding="utf-8"))

    @router.post("")
    def run(study: Study) -> dict:
        from syntera.optimise.optimiser import OPTIMISATION_FILE, optimise
        from syntera.optimise.report import REPORT_FILE, build_report
        from syntera.reporting.output import write_json

        try:
            problem = OptimisationProblem(design=str(config_path), **study.model_dump())
            design = load_design_input(config_path)
            check_problem(problem, design)
        except (ProblemError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        with lock:
            folder = root / "latest"
            try:
                result = optimise(problem, design, folder)
            except ProblemError as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
            report = build_report(problem, result)
            write_json(result, folder / OPTIMISATION_FILE)
            write_json(report, folder / REPORT_FILE)
        return report.model_dump(mode="json")

    return router
