"""Rationale records: why each value Syntera uses has the value it has.

Every value the pipeline chooses or accepts carries one record: where it came from (the
user, a schema default, a named rule, a calculation, a drawing, or an inference), the rule
and inputs behind it, and a short explanation template. Records are produced by
deterministic code only; they explain values and never set or change an assurance verdict.
"""

from __future__ import annotations

import string
from collections.abc import Iterable
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from syntera.schemas import DemoConfig

RATIONALE_SCHEMA_VERSION = 1
RATIONALE_FILE = "rationale.json"

RationaleSource = Literal["user", "default", "rule", "calculation", "drawing", "inferred"]
Scalar = bool | int | float | str
RationaleValue = Scalar | tuple[float, ...]
ParameterPath = Annotated[str, Field(pattern=r"^[A-Za-z0-9_\-]+(\.[A-Za-z0-9_\-]+)*$")]

SOURCE_LABELS: dict[str, str] = {
    "user": "Given by the user",
    "default": "Default value",
    "rule": "Set by a rule",
    "calculation": "Calculated",
    "drawing": "Read from the drawing",
    "inferred": "Inferred (not stated anywhere; challenge it if it looks wrong)",
}
# Sources whose value is produced by named, versioned code and must say which.
NEEDS_RULE: frozenset[str] = frozenset({"rule", "calculation"})


class Rationale(BaseModel):
    """Why one parameter has its value.

    ``explanation`` is a ``str.format`` template that may use ``{value}``, ``{unit}`` and
    any key of ``inputs``; it is checked when the record is built so it always renders.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: ParameterPath
    value: RationaleValue
    unit: str | None = None
    source: RationaleSource
    rule_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.]*\.v[0-9]+$")
    inputs: dict[str, RationaleValue] = Field(default_factory=dict)
    explanation: str = Field(min_length=1)
    references: tuple[str, ...] = ()

    @model_validator(mode="after")
    def must_be_traceable(self) -> Rationale:
        if self.source in NEEDS_RULE and self.rule_id is None:
            raise ValueError(f"{self.path}: a {self.source} rationale must name its rule_id")
        fields = {
            name for _, name, _, _ in string.Formatter().parse(self.explanation) if name is not None
        }
        allowed = {"value", "unit", *self.inputs}
        unknown = sorted(name for name in fields if name not in allowed)
        if unknown:
            raise ValueError(
                f"{self.path}: explanation uses unknown fields {unknown}; "
                f"allowed are {sorted(allowed)}"
            )
        return self

    def text(self) -> str:
        """Plain-language explanation, always prefixed with where the value came from."""
        body = self.explanation.format(
            value=_display(self.value), unit=self.unit or "", **self.inputs
        )
        body = " ".join(body.split()).replace(" .", ".").replace(" ;", ";")
        return f"{SOURCE_LABELS[self.source]}: {body}"


class RationaleReport(BaseModel):
    """All rationale records for one run, one per parameter path, sorted by path."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = RATIONALE_SCHEMA_VERSION
    subject: str = Field(min_length=1)
    entries: list[Rationale]

    @model_validator(mode="after")
    def paths_must_be_unique_and_sorted(self) -> RationaleReport:
        paths = [entry.path for entry in self.entries]
        duplicates = sorted({path for path in paths if paths.count(path) > 1})
        if duplicates:
            raise ValueError(f"duplicate rationale paths: {duplicates}")
        if paths != sorted(paths):
            raise ValueError("rationale entries must be sorted by path")
        return self

    def get(self, path: str) -> Rationale:
        for entry in self.entries:
            if entry.path == path:
                return entry
        raise KeyError(path)


def build_report(subject: str, *groups: Iterable[Rationale]) -> RationaleReport:
    """Merge groups of records; a later group overrides an earlier one at the same path."""
    merged: dict[str, Rationale] = {}
    for group in groups:
        for entry in group:
            merged[entry.path] = entry
    return RationaleReport(subject=subject, entries=[merged[path] for path in sorted(merged)])


def _display(value: RationaleValue) -> str:
    if isinstance(value, tuple):
        return " x ".join(_display(item) for item in value)
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


# ---- rationale for validated configuration models -------------------------------------

_MM_FIELDS = frozenset(
    {
        "outer_diameter",
        "minimum_bend_radius",
        "minimum_clearance",
        "voxel_resolution",
        "wall_thickness",
        "maximum_axial_element_length",
        "maximum_displacement_mm",
    }
)
_UNITS_BY_SUFFIX = (
    ("_mpa", "MPa"),
    ("_mm", "mm"),
    ("_per_k", "1/K"),
    ("_tonne_per_mm3", "t/mm3"),
)
_DESCRIPTIONS: dict[str, str] = {
    "tube.outer_diameter": "pipe outside diameter",
    "tube.minimum_bend_radius": "minimum centreline bend radius",
    "tube.minimum_clearance": "minimum clearance from the pipe surface to any obstacle",
    "voxel_resolution": "routing grid spacing",
    "routing_cost_weights.bend": "routing cost added for each bend (mm of pipe equivalent)",
    "routing_cost_weights.obstacle_proximity": "routing cost for running close to obstacles",
    "routing_cost_weights.port_direction": (
        "port-direction weight (recorded only: the router enforces port direction as a hard "
        "constraint, so this weight never trades against it)"
    ),
}


def unit_for(field: str) -> str | None:
    """Unit of a configuration field from its name (all lengths are millimetres)."""
    if field in _MM_FIELDS:
        return "mm"
    for suffix, unit in _UNITS_BY_SUFFIX:
        if field.endswith(suffix):
            return unit
    return None


def model_rationale(model: BaseModel, prefix: str) -> list[Rationale]:
    """``user`` or ``default`` records for every scalar leaf of a validated model.

    A field counts as given by the user only when it was present in the input (Pydantic's
    ``model_fields_set``); otherwise its schema default was used.
    """
    records: list[Rationale] = []
    for name in sorted(type(model).model_fields):
        value = getattr(model, name)
        path = f"{prefix}.{name}" if prefix else name
        if isinstance(value, BaseModel):
            records.extend(model_rationale(value, path))
            continue
        if value is None or not isinstance(value, bool | int | float | str):
            continue
        given = name in model.model_fields_set
        what = _DESCRIPTIONS.get(path, name.replace("_", " "))
        records.append(
            Rationale(
                path=path,
                value=value,
                unit=unit_for(name),
                source="user" if given else "default",
                explanation=(
                    f"{what} {{value}} {{unit}} as stated in the input configuration."
                    if given
                    else f"{what} {{value}} {{unit}} is the schema default; "
                    "the input did not state it."
                ),
            )
        )
    return records


def config_rationale(config: DemoConfig) -> list[Rationale]:
    """Records for the values a routing configuration sets.

    Covers the tube specification, routing grid, routing cost weights and, when present,
    the analysis specification. Geometry inputs (workspace, obstacles, ports) are the
    problem statement, not chosen values, and have no record here.
    """
    records: list[Rationale] = []
    for name in ("tube", "routing_cost_weights"):
        records.extend(model_rationale(getattr(config, name), name))
    records.append(
        Rationale(
            path="voxel_resolution",
            value=config.voxel_resolution,
            unit="mm",
            source="user",
            explanation="routing grid spacing {value} {unit} as stated in the input configuration.",
        )
    )
    analysis = config.analysis
    if analysis is not None:
        for name in ("wall_thickness", "material", "mesh", "acceptance"):
            value = getattr(analysis, name)
            if isinstance(value, BaseModel):
                records.extend(model_rationale(value, f"analysis.{name}"))
            else:
                records.append(
                    Rationale(
                        path=f"analysis.{name}",
                        value=value,
                        unit=unit_for(name),
                        source="user",
                        explanation=f"{name.replace('_', ' ')} {{value}} {{unit}} as stated "
                        "in the input configuration.",
                    )
                )
    return records


def grid_rationale(step: float, workspace: tuple[float, ...], max_voxels: int) -> Rationale:
    """Record for a routing grid chosen as the finest standard step under a voxel budget."""
    return Rationale(
        path="voxel_resolution",
        value=step,
        unit="mm",
        source="calculation",
        rule_id="grid.finest_standard_step.v1",
        inputs={"workspace_mm": tuple(float(v) for v in workspace), "max_voxels": max_voxels},
        explanation="routing grid {value} {unit} is the finest standard step that keeps a "
        "{workspace_mm} mm workspace within {max_voxels} grid nodes.",
    )


def weights_rationale(
    bend: float, proximity: float, port_direction: float, step: float, rule_id: str
) -> list[Rationale]:
    """Records for routing cost weights scaled from the routing grid by ``rule_id``."""
    inputs: dict[str, RationaleValue] = {"step_mm": step}
    return [
        Rationale(
            path="routing_cost_weights.bend",
            value=bend,
            source="rule",
            rule_id=rule_id,
            inputs={**inputs, "factor": bend / step},
            explanation="bend cost {value} is {factor:g} x the {step_mm:g} mm grid step, so "
            "a bend is worth that much extra pipe length when routes are ranked.",
        ),
        Rationale(
            path="routing_cost_weights.obstacle_proximity",
            value=proximity,
            source="rule",
            rule_id=rule_id,
            inputs={**inputs, "factor": proximity / step},
            explanation="obstacle-proximity cost {value} is {factor:g} x the {step_mm:g} mm "
            "grid step; it only ranks routes and never relaxes the clearance check.",
        ),
        Rationale(
            path="routing_cost_weights.port_direction",
            value=port_direction,
            source="rule",
            rule_id=rule_id,
            explanation="port-direction weight {value} is fixed; the router enforces port "
            "direction as a hard constraint.",
        ),
    ]
