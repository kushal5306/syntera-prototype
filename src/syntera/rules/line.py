"""Size a line from its process duty and fill the configuration values the rules choose.

A configuration may give a ``line_sizing`` block (medium, flow, design pressure and
temperature) instead of ``tube.outer_diameter``. The rules then choose the outside
diameter and, where the input does not state them, the bend radius, clearance, wall
thickness and material. Values the input states are kept and recorded as the user's; a
stated wall below the hoop-stress minimum is refused rather than overridden.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from syntera.rationale import Rationale, model_rationale
from syntera.rules.sizing import (
    RuleError,
    TubeChoice,
    allowable_stress_from_yield,
    bend_radius,
    clearance,
    maximum_velocity,
    select_material,
    select_tube,
)
from syntera.rules.tables import Medium
from syntera.schemas import AcceptanceThresholds, DemoConfig, Material

LINE_SIZING_KEY = "line_sizing"


class LineSizingSpec(BaseModel):
    """Process duty of one line (flow in m3/h, pressure in MPa, temperature in degC)."""

    model_config = ConfigDict(extra="forbid")

    medium: Medium
    flow_m3_per_h: Annotated[float, Field(gt=0)]
    design_pressure_mpa: Annotated[float, Field(ge=0)]
    design_temperature_c: float
    maximum_velocity_m_per_s: Annotated[float, Field(gt=0)] | None = None
    joint_efficiency: Annotated[float, Field(gt=0, le=1)] = 1.0
    corrosion_allowance_mm: Annotated[float, Field(ge=0)] = 0.0
    bend_radius_factor: Annotated[float, Field(ge=0.5)] = 1.5
    minimum_clearance_floor_mm: Annotated[float, Field(ge=0)] = 25.0


@dataclass(frozen=True)
class LineSizing:
    """Values the rules chose for one line, with every rationale record behind them."""

    tube: TubeChoice
    minimum_bend_radius: float
    minimum_clearance: float
    material: Material
    allowable_stress_mpa: float
    records: tuple[Rationale, ...]


def size_line(
    spec: LineSizingSpec,
    *,
    wall_thickness_mm: float | None = None,
    material: Material | None = None,
    stress_safety_factor: float = AcceptanceThresholds.model_fields["stress_safety_factor"].default,
) -> LineSizing:
    """Choose tube, bend radius, clearance and material for ``spec``.

    ``wall_thickness_mm`` and ``material`` are values the user already stated; they are
    used as given (a stated material's allowable stress is its yield over the safety
    factor) instead of being chosen from the tables.
    """
    records: list[Rationale] = []
    if spec.maximum_velocity_m_per_s is None:
        limit = maximum_velocity(spec.medium)
        velocity_limit = limit.value
        records.extend(limit.records)
    else:
        velocity_limit = spec.maximum_velocity_m_per_s
    if material is None:
        band = select_material(spec.medium, spec.design_temperature_c)
        chosen_material, allowable = band.value.material, band.value.allowable_stress_mpa
        records.extend(band.records)
    else:
        stress = allowable_stress_from_yield(material, stress_safety_factor)
        chosen_material, allowable = material, stress.value
        records.extend(stress.records)
    tube = select_tube(
        spec.flow_m3_per_h,
        velocity_limit,
        spec.design_pressure_mpa,
        allowable,
        joint_efficiency=spec.joint_efficiency,
        corrosion_allowance_mm=spec.corrosion_allowance_mm,
        wall_thickness_mm=wall_thickness_mm,
    )
    records.extend(tube.records)
    od = tube.value.outer_diameter
    bend = bend_radius(od, spec.bend_radius_factor)
    gap = clearance(od, spec.minimum_clearance_floor_mm)
    records.extend((*bend.records, *gap.records))
    return LineSizing(
        tube=tube.value,
        minimum_bend_radius=bend.value,
        minimum_clearance=gap.value,
        material=chosen_material,
        allowable_stress_mpa=allowable,
        records=tuple(records),
    )


def sized_config(raw: dict[str, Any]) -> tuple[DemoConfig, list[Rationale]]:
    """Validate a raw configuration, sizing its line first when it has ``line_sizing``.

    Returns the configuration and the rationale records to pass to ``execute_pipeline``
    so that rule-chosen values are never reported as stated by the user. Without a
    ``line_sizing`` block the configuration is validated unchanged and no records are
    added.
    """
    data = deepcopy(raw)
    if LINE_SIZING_KEY not in data:
        return DemoConfig.model_validate(data), []
    spec = LineSizingSpec.model_validate(data.pop(LINE_SIZING_KEY))
    tube = dict(data.get("tube") or {})
    if "outer_diameter" in tube:
        raise RuleError(
            "state either tube.outer_diameter or line_sizing, not both: the line size "
            "would otherwise have two sources"
        )
    analysis = data.get("analysis")
    analysis = dict(analysis) if isinstance(analysis, dict) else None
    stated_wall = None if analysis is None else analysis.get("wall_thickness")
    stated_material = None
    safety_factor = AcceptanceThresholds.model_fields["stress_safety_factor"].default
    if analysis is not None:
        if analysis.get("material") is not None:
            stated_material = Material.model_validate(analysis["material"])
        acceptance = analysis.get("acceptance")
        if isinstance(acceptance, dict) and "stress_safety_factor" in acceptance:
            safety_factor = float(acceptance["stress_safety_factor"])
    sizing = size_line(
        spec,
        wall_thickness_mm=None if stated_wall is None else float(stated_wall),
        material=stated_material,
        stress_safety_factor=safety_factor,
    )

    by_path = {record.path: record for record in sizing.records}
    keep = [record for record in sizing.records if not record.path.startswith("tube.")]
    tube["outer_diameter"] = sizing.tube.outer_diameter
    keep.append(by_path["tube.outer_diameter"])
    for field, value in (
        ("minimum_bend_radius", sizing.minimum_bend_radius),
        ("minimum_clearance", sizing.minimum_clearance),
    ):
        if field not in tube:
            tube[field] = value
            keep.append(by_path[f"tube.{field}"])
    data["tube"] = tube

    if analysis is not None:
        if stated_wall is None:
            analysis["wall_thickness"] = sizing.tube.wall_thickness
            keep.append(
                by_path["sizing.wall_thickness"].model_copy(
                    update={"path": "analysis.wall_thickness"}
                )
            )
        if stated_material is None:
            analysis["material"] = sizing.material.model_dump(exclude_none=True)
            keep.extend(_material_records(sizing.material, by_path["sizing.material"]))
        data["analysis"] = analysis

    config = DemoConfig.model_validate(data)
    return config, [*model_rationale(spec, LINE_SIZING_KEY), *keep]


def sizing_still_applies(sized: DemoConfig, config: DemoConfig) -> bool:
    """Whether ``config`` still carries every value sizing chose or depended on.

    A client that edits a sized configuration (the web app posts the configuration back)
    may change the tube, wall, material or safety factor. The sizing records explain the
    values in ``sized`` only, so they apply to ``config`` only while those values are
    unchanged; otherwise the edited values are the user's and must be reported as such.
    """
    if sized.tube != config.tube:
        return False
    before, after = sized.analysis, config.analysis
    if before is None or after is None:
        return before is after
    return (
        before.wall_thickness == after.wall_thickness
        and before.material == after.material
        and before.acceptance.stress_safety_factor == after.acceptance.stress_safety_factor
    )


def _material_records(material: Material, chosen: Rationale) -> list[Rationale]:
    """``rule`` records for every property of a material taken from the material table."""
    records = [chosen.model_copy(update={"path": "analysis.material.name"})]
    for record in model_rationale(material, "analysis.material"):
        if record.path == "analysis.material.name":
            continue
        what = record.path.rsplit(".", 1)[-1].replace("_", " ")
        records.append(
            Rationale(
                path=record.path,
                value=record.value,
                unit=record.unit,
                source="rule",
                rule_id=chosen.rule_id,
                inputs={"material": material.name},
                explanation=f"{what} {{value}} {{unit}} of {{material}} from the material table.",
                references=chosen.references,
            )
        )
    return records
