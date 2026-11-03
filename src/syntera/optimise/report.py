"""Optimisation report: chosen design, trade-offs and sensitivity, with rationale (H3)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from syntera.optimise.optimiser import Evaluation, OptimisationResult
from syntera.optimise.problem import OptimisationProblem
from syntera.rationale import Rationale

REPORT_FILE = "optimisation_report.json"
MARKDOWN_FILE = "optimisation_report.md"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TradeOff(StrictModel):
    candidate: str
    values: dict[str, float]
    objectives: dict[str, float]
    deltas: dict[str, float]


class Sensitivity(StrictModel):
    variable: str
    from_value: float
    to_value: float
    candidate: str
    feasible: bool
    deltas: dict[str, float] | None
    reasons: list[str]


class OptimisationReport(StrictModel):
    title: str
    strategy: str
    seed: int
    evaluated: int
    feasible: int
    rejected: int
    objectives: list[str]
    chosen: Evaluation | None
    pareto: list[TradeOff]
    sensitivity: list[Sensitivity]
    rejected_reasons: dict[str, int]
    rationale: list[Rationale]
    note: str = (
        "Every candidate was routed and verified; rejected candidates failed a check or a "
        "stated limit and are never traded off. Synthetic cost model and tables."
    )


def _deltas(a: dict[str, float], b: dict[str, float]) -> dict[str, float]:
    return {name: round(a[name] - b[name], 6) for name in b}


def build_report(problem: OptimisationProblem, result: OptimisationResult) -> OptimisationReport:
    by_id = {e.id: e for e in result.evaluations}
    chosen = by_id.get(result.chosen) if result.chosen else None
    pareto = []
    if chosen is not None and chosen.objectives is not None:
        for cid in result.pareto:
            member = by_id[cid]
            found = member.objectives or {}
            deltas = _deltas(found, chosen.objectives)
            pareto.append(
                TradeOff(candidate=cid, values=member.values, objectives=found, deltas=deltas)
            )
    sensitivity: list[Sensitivity] = []
    if chosen is not None and chosen.objectives is not None:
        steps = {v.name: v.values() for v in problem.variables}
        for name, grid in steps.items():
            here = chosen.values[name]
            position = min(range(len(grid)), key=lambda i: abs(grid[i] - here))
            for neighbour in (position - 1, position + 1):
                if not 0 <= neighbour < len(grid):
                    continue
                wanted = {**chosen.values, name: grid[neighbour]}
                match = next((e for e in result.evaluations if e.values == wanted), None)
                if match is None:
                    continue
                sensitivity.append(
                    Sensitivity(
                        variable=name, from_value=here, to_value=grid[neighbour],
                        candidate=match.id, feasible=match.feasible,
                        deltas=_deltas(match.objectives, chosen.objectives)
                        if match.feasible and match.objectives else None,
                        reasons=match.reasons,
                    )
                )  # fmt: skip
    rejected: dict[str, int] = {}
    for evaluation in result.evaluations:
        if not evaluation.feasible:
            key = evaluation.reasons[0].split(":")[0] if evaluation.reasons else "rejected"
            rejected[key] = rejected.get(key, 0) + 1
    feasible = sum(e.feasible for e in result.evaluations)
    return OptimisationReport(
        title=result.title,
        strategy=result.strategy,
        seed=result.seed,
        evaluated=result.evaluated,
        feasible=feasible,
        rejected=result.evaluated - feasible,
        objectives=list(problem.objectives),
        chosen=chosen,
        pareto=pareto,
        sensitivity=sensitivity,
        rejected_reasons=dict(sorted(rejected.items())),
        rationale=result.rationale,
    )


def _fmt(values: dict[str, float]) -> str:
    return ", ".join(f"{k} {v:g}" for k, v in values.items())


def report_markdown(report: OptimisationReport) -> str:
    lines = [f"# {report.title}", ""]
    lines.append(f"{report.evaluated} candidates evaluated ({report.strategy}, seed "
                 f"{report.seed}): {report.feasible} passed every check and limit, "
                 f"{report.rejected} rejected.")  # fmt: skip
    lines.append("")
    if report.chosen is None:
        lines.append("No candidate passed; there is no recommended design.")
    else:
        lines += ["## Chosen design", "",
                  f"Candidate {report.chosen.id}: {_fmt(report.chosen.values)}.", "",
                  f"Objectives: {_fmt(report.chosen.objectives or {})}.", ""]  # fmt: skip
        for record in report.rationale:
            lines += [f"Why: {record.text()}", ""]
        lines += ["## Trade-offs (Pareto set, change versus the chosen design)", "",
                  "| candidate | values | " + " | ".join(report.objectives) + " |",
                  "|---|---|" + "---|" * len(report.objectives)]  # fmt: skip
        for member in report.pareto:
            deltas = " | ".join(f"{member.deltas[n]:+g}" for n in report.objectives)
            lines.append(f"| {member.candidate} | {_fmt(member.values)} | {deltas} |")
        lines += ["", "## Sensitivity (one variable step from the chosen design)", ""]
        for item in report.sensitivity:
            if item.feasible and item.deltas is not None:
                effect = _fmt(item.deltas)
            else:
                effect = "rejected: " + "; ".join(item.reasons)
            lines.append(f"- {item.variable} {item.from_value:g} -> {item.to_value:g}: {effect}")
    if report.rejected_reasons:
        lines += ["", "## Rejected candidates", ""]
        lines += [f"- {reason}: {count}" for reason, count in report.rejected_reasons.items()]
    lines += ["", report.note, ""]
    return "\n".join(lines)
