"""Support and spool layout of a routed line, with rationale (roadmap B3)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from syntera.rationale import Rationale
from syntera.rules.sizing import RuleError
from syntera.rules.supports import maximum_support_span, place_supports, split_spools
from syntera.schemas import DemoConfig, Point3

SUPPORTS_FILE = "supports.json"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SupportPoint(StrictModel):
    id: str
    distance_mm: float
    position: Point3
    direction: tuple[float, float, float]


class SpoolRecord(StrictModel):
    id: str
    start_mm: float
    end_mm: float
    length_mm: float


class SupportLayout(StrictModel):
    """Where a line is supported and cut; rules choose both and explain them."""

    line: str
    maximum_span_mm: float
    supports: list[SupportPoint]
    spools: list[SpoolRecord]
    rationale: list[Rationale]


def support_layout(config: DemoConfig, points: list[Point3], line: str = "main") -> SupportLayout:
    spec = config.supports
    if spec is None:
        raise RuleError("the configuration has no supports section")
    records: list[Rationale] = []
    if spec.maximum_span_mm is not None:
        span = spec.maximum_span_mm
        records.append(
            Rationale(
                path="supports.maximum_span",
                value=span,
                unit="mm",
                source="user",
                explanation="maximum support span {value} {unit} as stated in the input.",
            )
        )
    else:
        analysis = config.analysis
        if analysis is None:
            raise RuleError("the span rule needs the line's wall and material (analysis)")
        result = maximum_support_span(
            config.tube.outer_diameter,
            analysis.wall_thickness,
            analysis.material,
            spec.contents_density_tonne_per_mm3,
            spec.deflection_limit_mm,
        )
        span = result.value
        records += result.records
    bend = config.tube.minimum_bend_radius
    placed = place_supports(points, bend, span)
    spools = split_spools(points, bend, spec.maximum_spool_length_mm, spec.cut_clearance_mm)
    records += [*placed.records, *spools.records]
    return SupportLayout(
        line=line,
        maximum_span_mm=span,
        supports=[
            SupportPoint(
                id=f"{line}-S{i}",
                distance_mm=s.distance_mm,
                position=s.point,
                direction=s.direction,
            )  # fmt: skip
            for i, s in enumerate(placed.value, start=1)
        ],
        spools=[
            SpoolRecord(
                id=f"{line}-SP{s.index}",
                start_mm=s.start_mm,
                end_mm=s.end_mm,
                length_mm=s.length_mm,
            )  # fmt: skip
            for s in spools.value
        ],
        rationale=sorted(records, key=lambda record: record.path),
    )
