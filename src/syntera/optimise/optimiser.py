"""Verified optimiser: seeded search where every candidate is routed and checked (H2).

Candidates come from the problem's search space in a fixed order (``grid``), a seeded
sample (``random``), or a small Bayesian optimiser (``bayes``: a Gaussian-process
surrogate with expected improvement over the remaining grid points, after a seeded
start). Each candidate is applied to the design as a change set and fully routed and
verified by the deterministic pipeline. A candidate that fails a check or a stated limit
is rejected with its reasons; only feasible candidates enter the Pareto set. The same
problem and seed always give the same result.
"""

from __future__ import annotations

import math
import random
from pathlib import Path

import numpy as np
from pydantic import BaseModel, ConfigDict

from syntera.changes import apply_change_set
from syntera.design import DesignDocument, DesignError
from syntera.optimise.problem import (
    OptimisationProblem,
    ProblemError,
    candidate_changes,
    search_space,
)
from syntera.rationale import Rationale

OPTIMISATION_FILE = "optimisation.json"
CHOICE_RULE = "optimise.knee_point.v1"
UNSUPPORTED = {"peak_stress": "peak_stress needs solved FEA per candidate, not supported yet"}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evaluation(StrictModel):
    id: str
    values: dict[str, float]
    feasible: bool
    reasons: list[str]
    objectives: dict[str, float] | None
    output: str


class OptimisationResult(StrictModel):
    title: str
    problem_sha256: str
    strategy: str
    seed: int
    evaluated: int
    search_space: int
    evaluations: list[Evaluation]
    pareto: list[str]
    chosen: str | None
    rationale: list[Rationale]


def _envelope_m3(design: DesignDocument) -> float:
    from syntera.geometry.cad import obstacle_shape

    lows, highs = [], []
    for item in design.equipment:
        box = obstacle_shape(item.geometry).BoundingBox()
        lows.append((box.xmin, box.ymin, box.zmin))
        highs.append((box.xmax, box.ymax, box.zmax))
    tubes = {line.id: line.tube.outer_diameter / 2 for line in design.lines}
    for route in design.routes:
        half = tubes[route.line]
        for point in route.points:
            lows.append(tuple(v - half for v in point))
            highs.append(tuple(v + half for v in point))
    if not lows:
        return 0.0
    size = [max(h[i] for h in highs) - min(lo[i] for lo in lows) for i in range(3)]
    return round(size[0] * size[1] * size[2] / 1e9, 9)


def objectives_of(problem: OptimisationProblem, design: DesignDocument) -> dict[str, float]:
    """Objective values of a routed design (all from the stored routes)."""
    tubes = {line.id: line.tube for line in design.lines}
    length = sum(route.length_mm for route in design.routes)
    bends = sum(len(route.points) - 2 for route in design.routes)
    cost = sum(
        route.length_mm / 1000 * problem.cost.per_metre_per_mm_od * tubes[route.line].outer_diameter
        + (len(route.points) - 2) * problem.cost.per_bend
        for route in design.routes
    )
    values = {"length": round(length, 6), "bends": float(bends), "cost": round(cost, 6)}
    if "volume" in problem.objectives:
        values["volume"] = _envelope_m3(design)
    return {name: values[name] for name in problem.objectives}


def evaluate(
    problem: OptimisationProblem,
    design: DesignDocument,
    values: dict[str, float],
    output: Path,
    index: int,
) -> Evaluation:
    """Apply, route and verify one candidate; reject it if any check or limit fails."""
    from syntera.multiline import route_design

    candidate_id = f"c{index:04d}"
    try:
        changed, _ = apply_change_set(design, candidate_changes(problem, design, values))
    except DesignError as error:
        reasons = [f"invalid design: {error}"]
        return Evaluation(id=candidate_id, values=values, feasible=False, reasons=reasons,
                          objectives=None, output="")  # fmt: skip
    run = route_design(changed, output / candidate_id)
    if not run.report.overall_pass:
        return Evaluation(id=candidate_id, values=values, feasible=False,
                          reasons=run.report.failure_reasons or ["checks failed"],
                          objectives=None, output=candidate_id)  # fmt: skip
    found = objectives_of(problem, run.design)
    limits = problem.constraints
    measured = {
        "length": sum(route.length_mm for route in run.design.routes),
        "bends": sum(len(route.points) - 2 for route in run.design.routes),
    }
    reasons = []
    if limits.max_length_mm is not None and measured["length"] > limits.max_length_mm + 1e-9:
        reasons.append(f"length {measured['length']:.1f} mm exceeds {limits.max_length_mm:g}")
    if limits.max_bends is not None and measured["bends"] > limits.max_bends:
        reasons.append(f"{measured['bends']} bends exceed {limits.max_bends}")
    if limits.max_volume_m3 is not None:
        volume = _envelope_m3(run.design)
        if volume > limits.max_volume_m3 + 1e-12:
            reasons.append(f"volume {volume:.4f} m3 exceeds {limits.max_volume_m3:g}")
    return Evaluation(id=candidate_id, values=values, feasible=not reasons, reasons=reasons,
                      objectives=found, output=candidate_id)  # fmt: skip


def pareto_ids(evaluations: list[Evaluation], objectives: list[str]) -> list[str]:
    """Feasible candidates no other feasible candidate dominates (all minimised)."""
    feasible = [e for e in evaluations if e.feasible and e.objectives is not None]

    def dominates(a: Evaluation, b: Evaluation) -> bool:
        pairs = [(a.objectives[name], b.objectives[name]) for name in objectives]  # type: ignore[index]
        return all(x <= y for x, y in pairs) and any(x < y for x, y in pairs)

    return sorted(e.id for e in feasible if not any(dominates(o, e) for o in feasible))


def knee_point(evaluations: list[Evaluation], ids: list[str], objectives: list[str]) -> str | None:
    """Pareto member with the smallest sum of objectives normalised over the Pareto set."""
    members = [e for e in evaluations if e.id in ids]
    if not members:
        return None
    spans = {}
    for name in objectives:
        values = [m.objectives[name] for m in members]  # type: ignore[index]
        spans[name] = (min(values), max(values) - min(values) or 1.0)

    def score(e: Evaluation) -> tuple[float, str]:
        total = sum((e.objectives[n] - spans[n][0]) / spans[n][1] for n in objectives)  # type: ignore[index]
        return (round(total, 12), e.id)

    return min(members, key=score).id


def _bayes_order(problem: OptimisationProblem, space, evaluate_at) -> None:
    """Seeded start, then expected improvement of a GP surrogate over the remaining grid."""
    rng = random.Random(problem.seed)
    names = [v.name for v in problem.variables]
    matrix = np.array([[point[n] for n in names] for point in space], dtype=float)
    span = matrix.max(axis=0) - matrix.min(axis=0)
    unit = (matrix - matrix.min(axis=0)) / np.where(span > 0, span, 1.0)
    remaining = list(range(len(space)))
    start = rng.sample(remaining, min(len(space), max(2, problem.budget // 3), problem.budget))
    scores: dict[int, float] = {}
    for index in start:
        remaining.remove(index)
        scores[index] = evaluate_at(index)
    while remaining and len(scores) < problem.budget:
        seen = sorted(scores)
        x = unit[seen]
        y = np.array([scores[i] for i in seen])
        mean, std = y.mean(), y.std() or 1.0
        target = (y - mean) / std

        def kernel(a, b):
            distance = ((a[:, None, :] - b[None, :, :]) ** 2).sum(axis=2)
            return np.exp(-distance / (2 * 0.3**2))

        k = kernel(x, x) + 1e-6 * np.eye(len(seen))
        weights = np.linalg.solve(k, target)
        candidates = unit[remaining]
        cross = kernel(candidates, x)
        mu = cross @ weights
        variance = np.clip(
            1 - np.einsum("ij,ji->i", cross, np.linalg.solve(k, cross.T)), 1e-12, None
        )
        sigma = np.sqrt(variance)
        best = target.min()
        z = (best - mu - 0.01) / sigma
        cdf = 0.5 * (1 + np.vectorize(math.erf)(z / math.sqrt(2)))
        pdf = np.exp(-0.5 * z**2) / math.sqrt(2 * math.pi)
        improvement = (best - mu - 0.01) * cdf + sigma * pdf
        pick = remaining[int(np.argmax(np.round(improvement, 12)))]
        remaining.remove(pick)
        scores[pick] = evaluate_at(pick)


def optimise(
    problem: OptimisationProblem, design: DesignDocument, output: Path
) -> OptimisationResult:
    for name in problem.objectives:
        if name in UNSUPPORTED:
            raise ProblemError(UNSUPPORTED[name])
    space = search_space(problem)
    output.mkdir(parents=True, exist_ok=True)
    evaluations: list[Evaluation] = []

    def evaluate_at(index: int) -> float:
        result = evaluate(problem, design, space[index], output, len(evaluations) + 1)
        evaluations.append(result)
        if not result.feasible or result.objectives is None:
            return 1e9  # only steers the search; infeasible designs never enter the results
        return float(sum(result.objectives.values()))

    if problem.strategy == "grid":
        for index in range(min(problem.budget, len(space))):
            evaluate_at(index)
    elif problem.strategy == "random":
        order = random.Random(problem.seed).sample(range(len(space)),
                                                   min(problem.budget, len(space)))  # fmt: skip
        for index in order:
            evaluate_at(index)
    else:
        _bayes_order(problem, space, evaluate_at)
    front = pareto_ids(evaluations, problem.objectives)
    chosen = knee_point(evaluations, front, problem.objectives)
    rationale = []
    if chosen is not None:
        rationale.append(
            Rationale(
                path="optimisation.chosen",
                value=chosen,
                source="rule",
                rule_id=CHOICE_RULE,
                inputs={
                    "pareto_size": len(front),
                    "evaluated": len(evaluations),
                    "objectives": ", ".join(problem.objectives),
                },  # fmt: skip
                explanation="candidate {value} is the Pareto member closest to the ideal: the "
                "smallest sum of its objectives ({objectives}) normalised over the "
                "{pareto_size} Pareto designs found among {evaluated} verified candidates.",
            )
        )
    return OptimisationResult(
        title=problem.title,
        problem_sha256=problem.sha256(),
        strategy=problem.strategy,
        seed=problem.seed,
        evaluated=len(evaluations),
        search_space=len(space),
        evaluations=evaluations,
        pareto=front,
        chosen=chosen,
        rationale=rationale,
    )
