"""Identical runs are reused from content-addressed storage, byte for byte (I2)."""

from __future__ import annotations

from pathlib import Path

from syntera.cache import RunCache
from syntera.design import design_from_yaml
from syntera.multiline import route_design
from syntera.optimise.optimiser import optimise
from syntera.optimise.problem import load_problem
from syntera.storage import LocalStorage

PROJECT = Path(__file__).parents[2]
FIXED = PROJECT / "examples" / "demo_skid.yaml"
COMPARED = ("design.json", "design_report.json", "lines/main/route.json",
            "lines/main/rationale.json", "lines/main/metrics.json")  # fmt: skip


def test_a_cached_run_restores_the_same_bytes_as_a_fresh_run(tmp_path):
    design = design_from_yaml(FIXED)
    cache = RunCache(LocalStorage(tmp_path / "store"))
    first = cache.route_design(design, tmp_path / "first")
    second = cache.route_design(design, tmp_path / "second")
    assert (first.hit, second.hit) == (False, True) and first.key == second.key
    assert (cache.hits, cache.misses) == (1, 1)
    route_design(design, tmp_path / "fresh")
    for name in COMPARED:
        fresh = (tmp_path / "fresh" / name).read_bytes()
        assert (tmp_path / "second" / name).read_bytes() == fresh, name
    assert second.report.overall_pass is True and second.design == first.design


def test_a_damaged_entry_is_recomputed(tmp_path):
    design = design_from_yaml(FIXED)
    cache = RunCache(LocalStorage(tmp_path / "store"))
    run = cache.route_design(design, tmp_path / "a")
    report = tmp_path / "store" / "objects" / run.key[:2] / run.key / "design_report.json"
    report.write_text(report.read_text().replace('"overall_pass": true', '"overall_pass": false'))
    again = cache.route_design(design, tmp_path / "b")
    assert again.hit is False and again.report.overall_pass is True


def test_optimiser_reuses_runs_without_changing_its_result(tmp_path):
    problem, design = load_problem(PROJECT / "examples" / "demo_optimisation.yaml")
    problem = problem.model_copy(update={"budget": 3})
    plain = optimise(problem, design, tmp_path / "plain")
    cache = RunCache(LocalStorage(tmp_path / "store"))
    cold = optimise(problem, design, tmp_path / "cold", cache)
    warm = optimise(problem, design, tmp_path / "warm", cache)
    assert cache.misses == 3 and cache.hits == 3
    assert plain.model_dump() == cold.model_dump() == warm.model_dump()
