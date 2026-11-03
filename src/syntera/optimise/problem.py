"""Optimisation problem schema: variables, objectives and constraints (roadmap H1).

Variables map onto design-document edits, so every candidate is an ordinary change set:

- ``line_size``: the line's outside diameter, from a list of choices;
- ``bend_radius_factor``: the line's bend radius as a multiple of its outside diameter;
- ``equipment_position``: an offset of one item of equipment (and its nozzles) along one
  axis.

Objectives are minimised. Constraints are hard limits checked after the deterministic
checks have passed; candidates outside them are infeasible, never penalised. The
assurance checks are always a constraint and cannot be switched off.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from syntera.changes import ChangeSet, MoveEquipment, SetParameter
from syntera.design import DesignDocument, load_design_input

ObjectiveName = Literal["length", "bends", "cost", "volume", "peak_stress"]
MAX_GRID_POINTS = 10_000


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LineSize(StrictModel):
    kind: Literal["line_size"] = "line_size"
    line: str
    choices: list[Annotated[float, Field(gt=0)]] = Field(min_length=1)

    @property
    def name(self) -> str:
        return f"{self.line}.outer_diameter"

    def values(self) -> list[float]:
        return sorted(set(self.choices))


class Range(StrictModel):
    low: float
    high: float
    step: Annotated[float, Field(gt=0)]

    @model_validator(mode="after")
    def ordered(self) -> Range:
        if self.high < self.low:
            raise ValueError("high must not be below low")
        if (self.high - self.low) / self.step > MAX_GRID_POINTS:
            raise ValueError("range has too many steps")
        return self

    def values(self) -> list[float]:
        count = int(math.floor((self.high - self.low) / self.step + 1e-9)) + 1
        return [round(self.low + i * self.step, 9) for i in range(count)]


class BendRadiusFactor(Range):
    kind: Literal["bend_radius_factor"] = "bend_radius_factor"
    line: str

    @model_validator(mode="after")
    def at_least_half(self) -> BendRadiusFactor:
        if self.low < 0.5:
            raise ValueError("a bend radius below half the OD would cut into the tube")
        return self

    @property
    def name(self) -> str:
        return f"{self.line}.bend_radius_factor"


class EquipmentPosition(Range):
    kind: Literal["equipment_position"] = "equipment_position"
    equipment: str
    axis: Literal["x", "y", "z"]

    @property
    def name(self) -> str:
        return f"{self.equipment}.{self.axis}"


Variable = Annotated[LineSize | BendRadiusFactor | EquipmentPosition, Field(discriminator="kind")]


class CostModel(StrictModel):
    """Synthetic piping cost: per metre of pipe (scaled by OD) and per bend."""

    currency: str = "EUR"
    per_metre_per_mm_od: Annotated[float, Field(ge=0)] = 2.0
    per_bend: Annotated[float, Field(ge=0)] = 15.0


class Constraints(StrictModel):
    """Hard limits; the exact assurance checks are always required on top of these."""

    max_length_mm: Annotated[float, Field(gt=0)] | None = None
    max_bends: Annotated[int, Field(ge=0)] | None = None
    max_volume_m3: Annotated[float, Field(gt=0)] | None = None


class OptimisationProblem(StrictModel):
    title: str = Field(min_length=1)
    design: str = Field(description="design.json or YAML config, relative to this file")
    variables: list[Variable] = Field(min_length=1)
    objectives: list[ObjectiveName] = Field(min_length=1)
    constraints: Constraints = Field(default_factory=Constraints)
    cost: CostModel = Field(default_factory=CostModel)
    strategy: Literal["grid", "random", "bayes"] = "grid"
    seed: int = 0
    budget: Annotated[int, Field(ge=1, le=500)] = 20

    @model_validator(mode="after")
    def unique(self) -> OptimisationProblem:
        names = [variable.name for variable in self.variables]
        if len(names) != len(set(names)):
            raise ValueError(f"each variable may appear once: {names}")
        if len(self.objectives) != len(set(self.objectives)):
            raise ValueError("objectives must be unique")
        return self

    def sha256(self) -> str:
        text = json.dumps(self.model_dump(mode="json"), sort_keys=True)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ProblemError(ValueError):
    """The problem does not fit its design."""


def load_problem(path: str | Path) -> tuple[OptimisationProblem, DesignDocument]:
    source = Path(path)
    problem = OptimisationProblem.model_validate(yaml.safe_load(source.read_text("utf-8")))
    design = load_design_input(source.parent / problem.design)
    check_problem(problem, design)
    return problem, design


def check_problem(problem: OptimisationProblem, design: DesignDocument) -> None:
    """Every variable must name something in the design; objectives must be computable."""
    lines = {line.id for line in design.lines}
    equipment = {item.id for item in design.equipment}
    for variable in problem.variables:
        if isinstance(variable, LineSize | BendRadiusFactor) and variable.line not in lines:
            raise ProblemError(f"variable {variable.name}: unknown line {variable.line}")
        if isinstance(variable, EquipmentPosition) and variable.equipment not in equipment:
            raise ProblemError(f"variable {variable.name}: unknown equipment")
    if "peak_stress" in problem.objectives and any(line.analysis is None for line in design.lines):
        raise ProblemError("peak_stress needs an analysis section on every line")


def search_space(problem: OptimisationProblem) -> list[dict[str, float]]:
    """Every combination of variable values, in a fixed order."""
    names = [variable.name for variable in problem.variables]
    grids = [variable.values() for variable in problem.variables]
    size = math.prod(len(values) for values in grids)
    if size > MAX_GRID_POINTS:
        raise ProblemError(f"search space has {size} points; at most {MAX_GRID_POINTS}")
    return [dict(zip(names, combo, strict=True)) for combo in itertools.product(*grids)]


def candidate_changes(
    problem: OptimisationProblem, design: DesignDocument, values: dict[str, float]
) -> ChangeSet:
    """The change set that sets ``values`` on ``design``."""
    edits: list = []
    lines = {line.id: line for line in design.lines}
    sizes = {v.line: values[v.name] for v in problem.variables if isinstance(v, LineSize)}
    factors = {v.line: values[v.name] for v in problem.variables if isinstance(v, BendRadiusFactor)}
    for line_id in sorted(set(sizes) | set(factors)):
        tube = lines[line_id].tube
        od = sizes.get(line_id, tube.outer_diameter)
        factor = factors.get(line_id, tube.minimum_bend_radius / tube.outer_diameter)
        reason = "optimisation candidate"
        if line_id in sizes:
            edits.append(SetParameter(path=f"lines.{line_id}.tube.outer_diameter",
                                      value=od, explanation=reason))  # fmt: skip
        edits.append(SetParameter(path=f"lines.{line_id}.tube.minimum_bend_radius",
                                  value=round(factor * od, 6), explanation=reason))  # fmt: skip
    for variable in problem.variables:
        if isinstance(variable, EquipmentPosition):
            offset = [0.0, 0.0, 0.0]
            offset["xyz".index(variable.axis)] = values[variable.name]
            if any(offset):
                edits.append(MoveEquipment(equipment=variable.equipment, offset=tuple(offset)))
    label = ", ".join(f"{name}={value:g}" for name, value in sorted(values.items()))
    return ChangeSet(description=f"Candidate {label}"[:500], edits=edits or [
        SetParameter(path="title", value=design.title)])  # fmt: skip
