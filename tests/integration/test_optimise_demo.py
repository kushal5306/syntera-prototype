"""Every candidate is generated and verified; the same seed gives the same result (H2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from syntera.cli import main
from syntera.optimise.optimiser import OPTIMISATION_FILE, optimise
from syntera.optimise.problem import ProblemError, load_problem

PROJECT = Path(__file__).parents[2]
EXAMPLE = PROJECT / "examples" / "demo_optimisation.yaml"


def _variant(tmp_path, **changes) -> Path:
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    data.update(changes)
    data["design"] = str(PROJECT / "examples" / "demo_skid.yaml")
    path = tmp_path / "problem.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _strip(result):
    return result.model_dump(mode="json")


def test_grid_search_verifies_every_candidate_and_reports_a_pareto_set(tmp_path):
    problem, design = load_problem(EXAMPLE)
    result = optimise(problem, design, tmp_path / "grid")
    assert result.evaluated == 9 == result.search_space
    for evaluation in result.evaluations:
        folder = tmp_path / "grid" / evaluation.id
        report = json.loads((folder / "design_report.json").read_text(encoding="utf-8"))
        if evaluation.feasible:  # feasible only when the exact checks passed
            assert report["overall_pass"] is True
        else:
            assert evaluation.reasons
    feasible = [e for e in result.evaluations if e.feasible]
    assert feasible and set(result.pareto) <= {e.id for e in feasible}
    assert result.chosen in result.pareto
    assert result.rationale[0].rule_id == "optimise.knee_point.v1"


@pytest.mark.parametrize("strategy", ["random", "bayes"])
def test_the_same_seed_gives_the_same_result(tmp_path, strategy):
    path = _variant(tmp_path, strategy=strategy, budget=4, seed=11)
    problem, design = load_problem(path)
    first = optimise(problem, design, tmp_path / "a")
    second = optimise(problem, design, tmp_path / "b")
    assert _strip(first) == _strip(second)
    assert first.evaluated == 4
    assert len({tuple(e.values.items()) for e in first.evaluations}) == 4  # no repeats


def test_limits_reject_candidates_instead_of_penalising_them(tmp_path):
    path = _variant(tmp_path, constraints={"max_bends": 0}, budget=3)
    problem, design = load_problem(path)
    result = optimise(problem, design, tmp_path / "out")
    assert all(not e.feasible for e in result.evaluations)
    assert all("bends exceed 0" in e.reasons[0] for e in result.evaluations)
    assert result.pareto == [] and result.chosen is None


def test_cli_and_unsupported_objectives(tmp_path, capsys):
    assert main(["optimise", "--problem", str(_variant(tmp_path, budget=2)),
                 "--output", str(tmp_path / "cli")]) == 0  # fmt: skip
    report = json.loads((tmp_path / "cli" / OPTIMISATION_FILE).read_text(encoding="utf-8"))
    assert report["evaluated"] == 2
    assert "Pareto" in capsys.readouterr().out
    problem, design = load_problem(_variant(tmp_path, objectives=["length", "peak_stress"]))
    with pytest.raises(ProblemError, match="peak_stress needs solved FEA"):
        optimise(problem, design, tmp_path / "x")
