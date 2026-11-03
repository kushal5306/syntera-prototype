"""Optimisation problem schema: variables, objectives, constraints (H1)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from syntera.changes import apply_change_set
from syntera.design import design_from_yaml
from syntera.optimise.problem import (
    OptimisationProblem,
    ProblemError,
    candidate_changes,
    check_problem,
    load_problem,
    search_space,
)

PROJECT = Path(__file__).parents[2]
EXAMPLE = PROJECT / "examples" / "demo_optimisation.yaml"
FIXED = design_from_yaml(PROJECT / "examples" / "demo_skid.yaml")
SIZE = {"kind": "line_size", "line": "main", "choices": [16, 20, 25, 20]}


def _problem(**changes) -> OptimisationProblem:
    data = {"title": "t", "design": "demo_skid.yaml", "objectives": ["length"],
            "variables": [SIZE]}  # fmt: skip
    data.update(changes)
    return OptimisationProblem.model_validate(data)


def test_example_problem_loads_and_spans_a_grid():
    problem, design = load_problem(EXAMPLE)
    assert design.title.startswith("Synthetic skid")
    space = search_space(problem)
    assert len(space) == 9
    assert space[0] == {"main.bend_radius_factor": 1.5, "auxiliary-pump.y": -20.0}
    assert space[-1] == {"main.bend_radius_factor": 2.5, "auxiliary-pump.y": 20.0}
    assert problem.sha256() == load_problem(EXAMPLE)[0].sha256()


def test_candidates_become_change_sets_on_the_design():
    problem, design = load_problem(EXAMPLE)
    values = {"main.bend_radius_factor": 2.0, "auxiliary-pump.y": -20.0}
    changed, _ = apply_change_set(design, candidate_changes(problem, design, values))
    assert changed.line("main").tube.minimum_bend_radius == 40  # 2 x OD 20
    pump = next(item for item in changed.equipment if item.id == "auxiliary-pump")
    original = next(item for item in design.equipment if item.id == "auxiliary-pump")
    assert pump.geometry.center[1] == original.geometry.center[1] - 20
    sized = _problem(variables=[SIZE, {"kind": "bend_radius_factor", "line": "main",
                                        "low": 1.5, "high": 1.5, "step": 1}])  # fmt: skip
    resized, _ = apply_change_set(FIXED, candidate_changes(sized, FIXED, {
        "main.outer_diameter": 25, "main.bend_radius_factor": 1.5}))  # fmt: skip
    assert resized.line("main").tube.outer_diameter == 25
    assert resized.line("main").tube.minimum_bend_radius == 37.5
    assert [v for v in SIZE["choices"]] and sized.variables[0].values() == [16, 20, 25]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"variables": [SIZE, SIZE]}, "each variable may appear once"),
        ({"objectives": []}, "at least 1"),
        ({"objectives": ["length", "length"]}, "unique"),
        ({"objectives": ["mass"]}, "Input should be"),
        (
            {
                "variables": [
                    {
                        "kind": "bend_radius_factor",
                        "line": "main",
                        "low": 0.2,
                        "high": 1,
                        "step": 0.1,
                    }
                ]
            },
            "cut into the tube",
        ),  # fmt: skip
        (
            {
                "variables": [
                    {
                        "kind": "equipment_position",
                        "equipment": "p",
                        "axis": "y",
                        "low": 0,
                        "high": 1e6,
                        "step": 1,
                    }
                ]
            },
            "too many steps",
        ),  # fmt: skip
        (
            {
                "variables": [
                    {
                        "kind": "equipment_position",
                        "equipment": "p",
                        "axis": "y",
                        "low": 5,
                        "high": 1,
                        "step": 1,
                    }
                ]
            },
            "high must not be below",
        ),  # fmt: skip
        ({"budget": 0}, "greater than or equal"),
    ],
)
def test_invalid_problems_are_refused(changes, message):
    with pytest.raises(ValidationError, match=message):
        _problem(**changes)


def test_problems_must_fit_their_design():
    with pytest.raises(ProblemError, match="unknown line"):
        check_problem(_problem(variables=[{**SIZE, "line": "x"}]), FIXED)
    with pytest.raises(ProblemError, match="unknown equipment"):
        check_problem(_problem(variables=[{"kind": "equipment_position", "equipment": "x",
                                           "axis": "z", "low": 0, "high": 0, "step": 1}]),
                      FIXED)  # fmt: skip
    data = FIXED.model_dump(mode="json")
    data["lines"][0]["analysis"] = None
    from syntera.design import parse_design

    with pytest.raises(ProblemError, match="peak_stress needs an analysis"):
        check_problem(_problem(objectives=["peak_stress"]), parse_design(data))
    check_problem(_problem(objectives=["length", "peak_stress"]), FIXED)
