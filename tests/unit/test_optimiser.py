"""Pareto set and knee point over verified candidates (H2)."""

from __future__ import annotations

from syntera.optimise.optimiser import Evaluation, knee_point, pareto_ids


def _e(cid, feasible=True, **objectives):
    return Evaluation(id=cid, values={}, feasible=feasible, reasons=[] if feasible else ["x"],
                      objectives=objectives if feasible else None, output=cid)  # fmt: skip


def test_pareto_keeps_only_feasible_non_dominated_candidates():
    evaluations = [
        _e("c1", length=100, bends=4),
        _e("c2", length=85, bends=5),
        _e("c3", length=120, bends=4),  # dominated by c1
        _e("c4", length=80, bends=6),
        _e("c5", feasible=False),  # rejected by a check: never in the set
        _e("c6", length=100, bends=4),  # equal to c1: neither dominates
    ]
    front = pareto_ids(evaluations, ["length", "bends"])
    assert front == ["c1", "c2", "c4", "c6"]
    # normalised over the front: c1 = 1 + 0, c2 = 0.25 + 0.5, c4 = 0 + 1, c6 = 1 + 0
    assert knee_point(evaluations, front, ["length", "bends"]) == "c2"
    assert knee_point(evaluations, [], ["length"]) is None


def test_single_objective_front_is_the_minimum():
    evaluations = [_e("c1", length=5), _e("c2", length=3), _e("c3", length=3)]
    front = pareto_ids(evaluations, ["length"])
    assert front == ["c2", "c3"]
    assert knee_point(evaluations, front, ["length"]) == "c2"
