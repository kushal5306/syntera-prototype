"""Pure, versioned sizing rules: each returns its value together with its rationale.

The formulas are deliberately simple and textbook-level (continuity for velocity, the
thin-wall hoop-stress formula for wall thickness). They choose design values; they never
decide whether a design is acceptable. Acceptance stays with the exact geometry checks and
the FEA thresholds, which are independent of these rules.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from syntera.rationale import Rationale, RationaleValue
from syntera.rules.tables import (
    MATERIAL_BANDS,
    MEDIUM_LIMITS,
    SYNTHETIC_TABLE_NOTICE,
    TUBE_SIZES,
    MaterialBand,
    Medium,
    TubeSize,
)
from syntera.schemas import Material

T = TypeVar("T")

RULE_LIBRARY_VERSION = 1
SECONDS_PER_HOUR = 3600.0
DIGITS = 4  # rounding of derived values, so records are stable and readable

MAX_VELOCITY_RULE = "rules.maximum_velocity_by_medium.v1"
INNER_DIAMETER_RULE = "rules.minimum_inner_diameter_from_velocity.v1"
VELOCITY_RULE = "rules.flow_velocity_from_flow_and_bore.v1"
WALL_RULE = "rules.wall_thickness_hoop_stress.v1"
TUBE_RULE = "rules.tube_size_from_flow.v1"
BEND_RULE = "rules.bend_radius_long_radius.v1"
CLEARANCE_RULE = "rules.clearance_half_od_with_floor.v1"
MATERIAL_RULE = "rules.material_by_medium_temperature.v1"
ALLOWABLE_RULE = "rules.allowable_stress_from_yield.v1"

HOOP_REFERENCE = "Thin-wall hoop stress (Barlow): t = P D / (2 S E) + c"
CONTINUITY_REFERENCE = "Continuity: v = Q / A, A = pi d^2 / 4"


class RuleError(ValueError):
    """A rule has no valid answer for its inputs; the message says why."""


@dataclass(frozen=True)
class RuleResult(Generic[T]):
    """A rule's value and the rationale records that explain it (first record is primary)."""

    value: T
    records: tuple[Rationale, ...]


def _r(value: float) -> float:
    return round(float(value), DIGITS)


def maximum_velocity(medium: Medium, path: str = "sizing.maximum_velocity") -> RuleResult[float]:
    """Velocity limit for a medium from the synthetic medium table."""
    limit = next(item for item in MEDIUM_LIMITS if item.medium == medium)
    value = limit.maximum_velocity_m_per_s
    return RuleResult(
        value,
        (
            Rationale(
                path=path,
                value=value,
                unit="m/s",
                source="rule",
                rule_id=MAX_VELOCITY_RULE,
                inputs={"medium": medium},
                explanation="maximum flow velocity {value} {unit} for {medium} from the "
                "medium table.",
                references=(SYNTHETIC_TABLE_NOTICE,),
            ),
        ),
    )


def minimum_inner_diameter(
    flow_m3_per_h: float, maximum_velocity_m_per_s: float
) -> RuleResult[float]:
    """Smallest bore (mm) that keeps ``flow`` at or below ``maximum_velocity``."""
    if flow_m3_per_h <= 0 or maximum_velocity_m_per_s <= 0:
        raise RuleError("flow and maximum velocity must both be positive")
    area_m2 = flow_m3_per_h / SECONDS_PER_HOUR / maximum_velocity_m_per_s
    value = _r(math.sqrt(4.0 * area_m2 / math.pi) * 1000.0)
    return RuleResult(
        value,
        (
            Rationale(
                path="sizing.minimum_inner_diameter",
                value=value,
                unit="mm",
                source="calculation",
                rule_id=INNER_DIAMETER_RULE,
                inputs={
                    "flow_m3_per_h": flow_m3_per_h,
                    "velocity_m_per_s": maximum_velocity_m_per_s,
                },
                explanation="a bore of at least {value} {unit} carries {flow_m3_per_h:g} m3/h "
                "at no more than {velocity_m_per_s:g} m/s.",
                references=(CONTINUITY_REFERENCE,),
            ),
        ),
    )


def flow_velocity(flow_m3_per_h: float, inner_diameter_mm: float) -> RuleResult[float]:
    """Mean flow velocity (m/s) of ``flow`` through a bore of ``inner_diameter_mm``."""
    if flow_m3_per_h <= 0 or inner_diameter_mm <= 0:
        raise RuleError("flow and bore must both be positive")
    area_m2 = math.pi * (inner_diameter_mm / 1000.0) ** 2 / 4.0
    value = _r(flow_m3_per_h / SECONDS_PER_HOUR / area_m2)
    return RuleResult(
        value,
        (
            Rationale(
                path="sizing.flow_velocity",
                value=value,
                unit="m/s",
                source="calculation",
                rule_id=VELOCITY_RULE,
                inputs={"flow_m3_per_h": flow_m3_per_h, "inner_diameter_mm": inner_diameter_mm},
                explanation="{flow_m3_per_h:g} m3/h through a {inner_diameter_mm:g} mm bore "
                "flows at {value} {unit}.",
                references=(CONTINUITY_REFERENCE,),
            ),
        ),
    )


def minimum_wall_thickness(
    pressure_mpa: float,
    outer_diameter_mm: float,
    allowable_stress_mpa: float,
    *,
    joint_efficiency: float = 1.0,
    corrosion_allowance_mm: float = 0.0,
) -> RuleResult[float]:
    """Thin-wall hoop-stress minimum wall: ``t = P D / (2 S E) + c`` (mm)."""
    if pressure_mpa < 0 or outer_diameter_mm <= 0 or allowable_stress_mpa <= 0:
        raise RuleError("pressure must be >= 0; diameter and allowable stress must be > 0")
    if not 0 < joint_efficiency <= 1:
        raise RuleError("joint efficiency must be in (0, 1]")
    if corrosion_allowance_mm < 0:
        raise RuleError("corrosion allowance must be >= 0")
    pressure_wall = (
        pressure_mpa * outer_diameter_mm / (2.0 * allowable_stress_mpa * joint_efficiency)
    )
    value = _r(pressure_wall + corrosion_allowance_mm)
    return RuleResult(
        value,
        (
            Rationale(
                path="sizing.minimum_wall_thickness",
                value=value,
                unit="mm",
                source="calculation",
                rule_id=WALL_RULE,
                inputs={
                    "pressure_mpa": pressure_mpa,
                    "outer_diameter_mm": outer_diameter_mm,
                    "allowable_stress_mpa": allowable_stress_mpa,
                    "joint_efficiency": joint_efficiency,
                    "corrosion_allowance_mm": corrosion_allowance_mm,
                },
                explanation="minimum wall {value} {unit} = {pressure_mpa:g} MPa x "
                "{outer_diameter_mm:g} mm / (2 x {allowable_stress_mpa:g} MPa x "
                "{joint_efficiency:g}) + {corrosion_allowance_mm:g} mm corrosion allowance.",
                references=(HOOP_REFERENCE,),
            ),
        ),
    )


def bend_radius(outer_diameter_mm: float, factor: float = 1.5) -> RuleResult[float]:
    """Long-radius bend: centreline radius ``factor`` x OD (never below the tube radius)."""
    if outer_diameter_mm <= 0 or factor < 0.5:
        raise RuleError("diameter must be positive and the factor at least 0.5")
    value = _r(factor * outer_diameter_mm)
    return RuleResult(
        value,
        (
            Rationale(
                path="tube.minimum_bend_radius",
                value=value,
                unit="mm",
                source="rule",
                rule_id=BEND_RULE,
                inputs={"factor": factor, "outer_diameter_mm": outer_diameter_mm},
                explanation="bend radius {value} {unit} is {factor:g} x the "
                "{outer_diameter_mm:g} mm OD (long-radius bend).",
            ),
        ),
    )


def clearance(outer_diameter_mm: float, minimum_mm: float = 25.0) -> RuleResult[float]:
    """Clearance to anything else: half the OD, never less than ``minimum_mm``."""
    if outer_diameter_mm <= 0 or minimum_mm < 0:
        raise RuleError("diameter must be positive and the floor non-negative")
    value = _r(max(minimum_mm, outer_diameter_mm / 2.0))
    return RuleResult(
        value,
        (
            Rationale(
                path="tube.minimum_clearance",
                value=value,
                unit="mm",
                source="rule",
                rule_id=CLEARANCE_RULE,
                inputs={"outer_diameter_mm": outer_diameter_mm, "minimum_mm": minimum_mm},
                explanation="clearance {value} {unit} is half the {outer_diameter_mm:g} mm OD, "
                "and never less than {minimum_mm:g} mm.",
            ),
        ),
    )


def select_material(medium: Medium, design_temperature_c: float) -> RuleResult[MaterialBand]:
    """First material band for ``medium`` whose temperature limit covers the design."""
    for band in MATERIAL_BANDS:
        if band.medium == medium and design_temperature_c <= band.maximum_temperature_c:
            return RuleResult(
                band,
                (
                    Rationale(
                        path="sizing.material",
                        value=band.material.name,
                        source="rule",
                        rule_id=MATERIAL_RULE,
                        inputs={
                            "medium": medium,
                            "temperature_c": design_temperature_c,
                            "band_limit_c": band.maximum_temperature_c,
                        },
                        explanation="{value} for {medium} at {temperature_c:g} degC "
                        "(material table band up to {band_limit_c:g} degC).",
                        references=(SYNTHETIC_TABLE_NOTICE,),
                    ),
                    Rationale(
                        path="sizing.allowable_stress",
                        value=band.allowable_stress_mpa,
                        unit="MPa",
                        source="rule",
                        rule_id=MATERIAL_RULE,
                        inputs={
                            "material": band.material.name,
                            "band_limit_c": band.maximum_temperature_c,
                        },
                        explanation="allowable stress {value} {unit} for {material} up to "
                        "{band_limit_c:g} degC from the material table.",
                        references=(SYNTHETIC_TABLE_NOTICE,),
                    ),
                ),
            )
    limits = sorted(b.maximum_temperature_c for b in MATERIAL_BANDS if b.medium == medium)
    raise RuleError(
        f"no material in the synthetic table for {medium} at {design_temperature_c:g} degC "
        f"(table covers up to {limits[-1]:g} degC)"
    )


def allowable_stress_from_yield(material: Material, safety_factor: float) -> RuleResult[float]:
    """Allowable stress for a user-stated material: yield strength / safety factor."""
    if safety_factor < 1:
        raise RuleError("safety factor must be at least 1")
    value = _r(material.yield_strength_mpa / safety_factor)
    return RuleResult(
        value,
        (
            Rationale(
                path="sizing.allowable_stress",
                value=value,
                unit="MPa",
                source="calculation",
                rule_id=ALLOWABLE_RULE,
                inputs={
                    "material": material.name,
                    "yield_mpa": material.yield_strength_mpa,
                    "safety_factor": safety_factor,
                },
                explanation="allowable stress {value} {unit} is the {yield_mpa:g} MPa yield "
                "strength of {material} divided by the {safety_factor:g} stress safety factor.",
            ),
        ),
    )


@dataclass(frozen=True)
class TubeChoice:
    """Selected standard tube and the numbers that justified it (mm, m/s)."""

    outer_diameter: float
    wall_thickness: float
    inner_diameter: float
    velocity: float
    minimum_wall_thickness: float


def select_tube(
    flow_m3_per_h: float,
    maximum_velocity_m_per_s: float,
    pressure_mpa: float,
    allowable_stress_mpa: float,
    *,
    joint_efficiency: float = 1.0,
    corrosion_allowance_mm: float = 0.0,
    wall_thickness_mm: float | None = None,
    sizes: tuple[TubeSize, ...] = TUBE_SIZES,
) -> RuleResult[TubeChoice]:
    """Smallest standard tube whose bore keeps velocity within the limit at a safe wall.

    Sizes are tried in ascending OD. For each, the wall is ``wall_thickness_mm`` when the
    user stated one, otherwise the thinnest stocked wall; the size is rejected when that
    wall is below the hoop-stress minimum or leaves no bore. Ties cannot occur because
    sizes are distinct and tried in a fixed order.
    """
    required_bore = minimum_inner_diameter(flow_m3_per_h, maximum_velocity_m_per_s)
    rejected: list[str] = []
    for size in sorted(sizes, key=lambda item: item.outer_diameter):
        od = size.outer_diameter
        wall_min = minimum_wall_thickness(
            pressure_mpa,
            od,
            allowable_stress_mpa,
            joint_efficiency=joint_efficiency,
            corrosion_allowance_mm=corrosion_allowance_mm,
        )
        if wall_thickness_mm is not None:
            wall: float | None = wall_thickness_mm if wall_thickness_mm >= wall_min.value else None
        else:
            wall = next((w for w in sorted(size.walls) if w >= wall_min.value), None)
        if wall is None or 2 * wall >= od:
            rejected.append(f"OD {od:g}: no wall >= {wall_min.value:g} mm")
            continue
        bore = _r(od - 2 * wall)
        if bore < required_bore.value:
            rejected.append(f"OD {od:g}: bore {bore:g} < {required_bore.value:g} mm")
            continue
        velocity = flow_velocity(flow_m3_per_h, bore)
        choice = TubeChoice(od, wall, bore, velocity.value, wall_min.value)
        inputs: dict[str, RationaleValue] = {
            "flow_m3_per_h": flow_m3_per_h,
            "velocity_m_per_s": velocity.value,
            "limit_m_per_s": maximum_velocity_m_per_s,
            "wall_mm": wall,
            "bore_mm": bore,
        }
        primary = Rationale(
            path="tube.outer_diameter",
            value=od,
            unit="mm",
            source="rule",
            rule_id=TUBE_RULE,
            inputs=inputs,
            explanation="OD {value} {unit} is the smallest standard tube whose "
            "{bore_mm:g} mm bore (wall {wall_mm:g} mm) carries {flow_m3_per_h:g} m3/h at "
            "{velocity_m_per_s:g} m/s, within the {limit_m_per_s:g} m/s limit.",
            references=(SYNTHETIC_TABLE_NOTICE, CONTINUITY_REFERENCE),
        )
        wall_record = Rationale(
            path="sizing.wall_thickness",
            value=wall,
            unit="mm",
            source="user" if wall_thickness_mm is not None else "rule",
            rule_id=None if wall_thickness_mm is not None else TUBE_RULE,
            inputs={"minimum_mm": wall_min.value},
            explanation=(
                "wall {value} {unit} as stated in analysis.wall_thickness; it is at least "
                "the {minimum_mm:g} mm hoop-stress minimum."
                if wall_thickness_mm is not None
                else "wall {value} {unit} is the thinnest stocked wall at least the "
                "{minimum_mm:g} mm hoop-stress minimum."
            ),
        )
        return RuleResult(
            choice,
            (primary, wall_record, *required_bore.records, *wall_min.records, *velocity.records),
        )
    raise RuleError(
        f"no standard tube carries {flow_m3_per_h:g} m3/h within {maximum_velocity_m_per_s:g} "
        f"m/s at {pressure_mpa:g} MPa: " + "; ".join(rejected)
    )


# Registry of every rule, keyed by its versioned id. A change to a rule's behaviour must
# bump the id's version so recorded rationale stays traceable to the code that wrote it.
RULES: dict[str, Callable[..., RuleResult]] = {
    MAX_VELOCITY_RULE: maximum_velocity,
    INNER_DIAMETER_RULE: minimum_inner_diameter,
    VELOCITY_RULE: flow_velocity,
    WALL_RULE: minimum_wall_thickness,
    TUBE_RULE: select_tube,
    BEND_RULE: bend_radius,
    CLEARANCE_RULE: clearance,
    MATERIAL_RULE: select_material,
    ALLOWABLE_RULE: allowable_stress_from_yield,
}
