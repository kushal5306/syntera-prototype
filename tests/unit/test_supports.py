"""Support span, support placement and spool splitting rules (B3)."""

from __future__ import annotations

import math

import pytest

from syntera.rules.sizing import RuleError
from syntera.rules.supports import (
    PLACEMENT_RULE,
    SPAN_RULE,
    SPOOL_RULE,
    maximum_support_span,
    place_supports,
    split_spools,
)
from syntera.schemas import Material

STEEL = Material(name="stainless_316", elastic_modulus_mpa=193000, poisson_ratio=0.3,
                 yield_strength_mpa=205, density_tonne_per_mm3=8.0e-9,
                 thermal_expansion_per_k=1.6e-5)  # fmt: skip
STRAIGHT = [(0.0, 0.0, 0.0), (1000.0, 0.0, 0.0)]
ELL = [(0.0, 0.0, 0.0), (600.0, 0.0, 0.0), (600.0, 500.0, 0.0)]


def test_span_from_the_deflection_limit_is_hand_checked():
    result = maximum_support_span(20, 2, STEEL, 1.0e-9, 2.5)
    metal, bore = math.pi * (10**2 - 8**2), math.pi * 8**2
    weight = (8.0e-9 * metal + 1.0e-9 * bore) * 9810
    inertia = math.pi / 4 * (10**4 - 8**4)
    exact = (384 * 193000 * inertia * 2.5 / (5 * weight)) ** 0.25
    assert exact == pytest.approx(1994.97, abs=0.05)
    assert result.value == 1990
    (record,) = result.records
    assert record.rule_id == SPAN_RULE and record.source == "calculation"
    assert "sags at most 2.5 mm" in record.text()
    empty = maximum_support_span(20, 2, STEEL, 0.0, 2.5).value
    assert empty > result.value  # an empty pipe can span further
    with pytest.raises(RuleError):
        maximum_support_span(20, 10, STEEL, 0.0, 2.5)


def test_supports_keep_every_span_within_the_maximum():
    result = place_supports(STRAIGHT, 30, 300)
    assert [s.distance_mm for s in result.value] == [300, 600, 900]
    assert [s.point for s in result.value] == [(300, 0, 0), (600, 0, 0), (900, 0, 0)]
    (record,) = result.records
    assert record.rule_id == PLACEMENT_RULE and record.value == 3
    assert place_supports(STRAIGHT, 30, 1000).value == []


def test_supports_sit_on_straight_runs_never_on_bends():
    radius = 100
    result = place_supports(ELL, radius, 520)
    arc_start, arc_end = 600 - radius, 600 - radius + math.pi / 2 * radius
    total = arc_end + 500 - radius
    marks = [0.0, *[s.distance_mm for s in result.value], total]
    assert all(b - a <= 520 + 1e-6 for a, b in zip(marks, marks[1:], strict=False))
    for station in result.value:
        assert not arc_start + 1e-6 < station.distance_mm < arc_end - 1e-6
    with pytest.raises(RuleError, match="no straight run within 100 mm"):
        place_supports([(0.0, 0, 0), (300.0, 0, 0), (300.0, 300, 0)], 200, 100)


def test_spools_are_split_within_the_maximum_and_clear_of_bends():
    result = split_spools(STRAIGHT, 30, 400, 50)
    assert [s.length_mm for s in result.value] == [400, 400, 200]
    assert result.records[0].rule_id == SPOOL_RULE and result.records[0].value == 3
    radius, clearance = 100, 50
    spools = split_spools(ELL, radius, 450, clearance).value
    arc_start = 600 - radius
    arc_end = arc_start + math.pi / 2 * radius
    for spool in spools[:-1]:
        cut = spool.end_mm
        assert not arc_start - clearance + 1e-6 < cut < arc_end + clearance - 1e-6
    assert all(s.length_mm <= 450 + 1e-6 for s in spools)
    assert split_spools(STRAIGHT, 30, 2000, 50).value[0].length_mm == 1000
