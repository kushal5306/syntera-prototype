"""Pipe support and spool rules (roadmap B3): pure functions returning value + rationale.

- maximum support span from a deflection limit (simply supported beam under its own and
  its contents' weight);
- support positions along the centreline so no span exceeds the maximum, on straight
  runs only;
- spool splitting so no spool exceeds a maximum length, cutting straight runs only and
  never closer than a weld clearance to a bend.

The formulae are textbook beam theory with synthetic defaults. They place supports and
cuts; they never decide whether a design is acceptable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from syntera.geometry.centreline import LineSegment, centreline_segments
from syntera.rationale import Rationale
from syntera.rules.sizing import RuleError, RuleResult
from syntera.schemas import Material, Point3

SPAN_RULE = "rules.support_span_deflection.v1"
PLACEMENT_RULE = "rules.support_placement_max_span.v1"
SPOOL_RULE = "rules.spool_split_max_length.v1"
GRAVITY_MM_PER_S2 = 9810.0
SPAN_STEP_MM = 10.0
BEAM_REFERENCE = "Simply supported beam, uniform load: deflection = 5 w L^4 / (384 E I)"
_EPS = 1e-6


def maximum_support_span(
    outer_diameter_mm: float,
    wall_mm: float,
    material: Material,
    contents_density_tonne_per_mm3: float,
    deflection_limit_mm: float,
) -> RuleResult[float]:
    """Longest span (rounded down to 10 mm) whose mid-span sag stays within the limit."""
    if wall_mm <= 0 or wall_mm * 2 >= outer_diameter_mm or deflection_limit_mm <= 0:
        raise RuleError("wall must be positive and below the radius; the limit positive")
    if material.density_tonne_per_mm3 is None:
        raise RuleError("material density is needed to weigh the pipe")
    outer = outer_diameter_mm / 2
    inner = outer - wall_mm
    metal_area = math.pi * (outer**2 - inner**2)
    bore_area = math.pi * inner**2
    inertia = math.pi / 4 * (outer**4 - inner**4)
    weight = (
        material.density_tonne_per_mm3 * metal_area + contents_density_tonne_per_mm3 * bore_area
    ) * GRAVITY_MM_PER_S2
    raw = (
        384 * material.elastic_modulus_mpa * inertia * deflection_limit_mm / (5 * weight)
    ) ** 0.25
    value = math.floor(raw / SPAN_STEP_MM) * SPAN_STEP_MM
    return RuleResult(
        value,
        (
            Rationale(
                path="supports.maximum_span",
                value=value,
                unit="mm",
                source="calculation",
                rule_id=SPAN_RULE,
                inputs={
                    "weight_n_per_mm": round(weight, 8),
                    "inertia_mm4": round(inertia, 3),
                    "elastic_modulus_mpa": material.elastic_modulus_mpa,
                    "deflection_limit_mm": deflection_limit_mm,
                },
                explanation="maximum support span {value} {unit}: a simply supported span of "
                "this length sags at most {deflection_limit_mm:g} mm under "
                "{weight_n_per_mm:g} N/mm of pipe and contents (E {elastic_modulus_mpa:g} MPa, "
                "I {inertia_mm4:g} mm4), rounded down to 10 mm.",
                references=(BEAM_REFERENCE,),
            ),
        ),
    )


@dataclass(frozen=True)
class Station:
    """A point on the centreline: distance along it, position and local direction."""

    distance_mm: float
    point: Point3
    direction: tuple[float, float, float]


def _straights(points: list[Point3], bend_radius: float, clearance: float = 0.0):
    """Straight intervals (start, end distance, segment) shrunk by ``clearance`` at bends."""
    segments = centreline_segments(points, bend_radius)
    intervals = []
    position = 0.0
    for index, segment in enumerate(segments):
        start, end = position, position + segment.length
        if isinstance(segment, LineSegment):
            lo = start + (clearance if index > 0 else 0.0)
            hi = end - (clearance if index < len(segments) - 1 else 0.0)
            if hi - lo > _EPS:
                intervals.append((lo, hi, start, segment))
        position = end
    return intervals, position


def _station(distance: float, intervals) -> Station:
    for lo, hi, start, segment in intervals:
        if lo - _EPS <= distance <= hi + _EPS:
            point = segment.point_at((distance - start) / segment.length)
            return Station(
                round(distance, 6),
                tuple(round(v, 6) + 0.0 for v in point),  # type: ignore[arg-type]
                segment.direction,
            )
    raise RuleError(f"no straight run at {distance:.3f} mm")


def _greedy(intervals, total: float, limit: float, what: str) -> list[float]:
    """Distances so no gap between consecutive marks (and the ends) exceeds ``limit``."""
    marks: list[float] = []
    last = 0.0
    while total - last > limit + _EPS:
        reach = last + limit
        candidates = [min(hi, reach) for lo, hi, _, _ in intervals if lo <= reach + _EPS]
        candidates = [c for c in candidates if c > last + _EPS]
        if not candidates:
            raise RuleError(f"no straight run within {limit:g} mm of {last:.1f} mm for a {what}")
        last = max(candidates)
        marks.append(last)
    return marks


def place_supports(
    points: list[Point3], bend_radius: float, maximum_span_mm: float
) -> RuleResult[list[Station]]:
    """Supports on straight runs so no span between supports or end anchors exceeds the max.

    The two ends are anchored at their ports. Walking from the start, each support goes as
    far along as the span allows, on the nearest straight run.
    """
    if maximum_span_mm <= 0:
        raise RuleError("maximum span must be positive")
    intervals, total = _straights(points, bend_radius)
    marks = _greedy(intervals, total, maximum_span_mm, "support")
    stations = [_station(mark, intervals) for mark in marks]
    spans = [b - a for a, b in zip([0.0, *marks], [*marks, total], strict=True)]
    longest = round(max(spans), 3)
    return RuleResult(
        stations,
        (
            Rationale(
                path="supports.count",
                value=len(stations),
                source="rule",
                rule_id=PLACEMENT_RULE,
                inputs={
                    "maximum_span_mm": maximum_span_mm,
                    "centreline_mm": round(total, 3),
                    "longest_span_mm": longest,
                },  # fmt: skip
                explanation="{value} support(s) on straight runs keep every span at or below "
                "{maximum_span_mm:g} mm along the {centreline_mm:g} mm centreline (longest "
                "span {longest_span_mm:g} mm); the line ends are anchored at their ports.",
            ),
        ),
    )


@dataclass(frozen=True)
class Spool:
    index: int
    start_mm: float
    end_mm: float

    @property
    def length_mm(self) -> float:
        return round(self.end_mm - self.start_mm, 6)


def split_spools(
    points: list[Point3], bend_radius: float, maximum_length_mm: float, cut_clearance_mm: float
) -> RuleResult[list[Spool]]:
    """Cut the line into spools no longer than the maximum, on straight runs only.

    Cuts keep ``cut_clearance_mm`` of straight pipe from every bend so each weld is clear
    of the bend's heat-affected zone.
    """
    if maximum_length_mm <= 0 or cut_clearance_mm < 0:
        raise RuleError("maximum spool length must be positive and the clearance non-negative")
    intervals, total = _straights(points, bend_radius, cut_clearance_mm)
    cuts = _greedy(intervals, total, maximum_length_mm, "spool cut")
    bounds = [0.0, *cuts, total]
    spools = [Spool(i + 1, round(a, 6), round(b, 6)) for i, (a, b) in
              enumerate(zip(bounds, bounds[1:], strict=False))]  # fmt: skip
    return RuleResult(
        spools,
        (
            Rationale(
                path="spools.count",
                value=len(spools),
                source="rule",
                rule_id=SPOOL_RULE,
                inputs={
                    "maximum_length_mm": maximum_length_mm,
                    "cut_clearance_mm": cut_clearance_mm,
                    "centreline_mm": round(total, 3),
                },  # fmt: skip
                explanation="{value} spool(s), each at most {maximum_length_mm:g} mm of "
                "centreline, cut on straight runs at least {cut_clearance_mm:g} mm from any bend "
                "({centreline_mm:g} mm line).",
            ),
        ),
    )
