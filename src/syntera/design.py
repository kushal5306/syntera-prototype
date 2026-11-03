"""Canonical design document: the versioned source of truth for one design.

A design document (``design.json``) holds the equipment, nozzles, lines, routing
parameters, the rationale behind every chosen value and the hashes of the inputs it was
made from. Run configurations are derived from it, never the other way round; existing
YAML configurations convert into it with :func:`design_from_yaml`.

The document is written as canonical JSON (sorted keys, two-space indent, trailing
newline) so identical input gives byte-identical files. It records design values only:
routes and assurance verdicts are outputs of the deterministic pipeline, and nothing in
the document can set or change a verdict.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from syntera.config import load_sized_config
from syntera.rationale import Rationale, build_report, config_rationale
from syntera.rules.line import LINE_SIZING_KEY, LineSizingSpec
from syntera.schemas import (
    AnalysisSpecification,
    DemoConfig,
    Obstacle,
    Point3,
    Port,
    RoutingWeights,
    StepObstacle,
    TubeSpecification,
    Workspace,
)

DESIGN_SCHEMA_VERSION = 1
DESIGN_FILE = "design.json"

DesignId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]
EquipmentId = Annotated[str, Field(min_length=1, max_length=128)]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

# Rationale paths for design-wide parameters live under ``parameters.``; every other
# record belongs to a line and lives under ``lines.<line id>.``.
_PARAMETER_ROOTS = frozenset({"voxel_resolution", "routing_cost_weights"})


class DesignError(ValueError):
    """A design document or conversion request that cannot be used as given."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Equipment(StrictModel):
    """One item of equipment and the exact geometry it occupies (mm)."""

    id: EquipmentId
    geometry: Obstacle

    @model_validator(mode="after")
    def geometry_name_matches_id(self) -> Equipment:
        if self.geometry.name != self.id:
            raise ValueError(f"equipment {self.id}: geometry name must equal the equipment id")
        return self


class Nozzle(StrictModel):
    """A connection point; ``equipment`` is ``None`` for a free (battery-limit) port."""

    id: DesignId
    equipment: EquipmentId | None = None
    port: Port


class Line(StrictModel):
    """One pipe line between two nozzles, with its tube and optional duty and analysis."""

    id: DesignId
    from_nozzle: DesignId
    to_nozzle: DesignId
    tube: TubeSpecification
    line_sizing: LineSizingSpec | None = None
    analysis: AnalysisSpecification | None = None

    @model_validator(mode="after")
    def ends_must_differ(self) -> Line:
        if self.from_nozzle == self.to_nozzle:
            raise ValueError(f"line {self.id}: from_nozzle and to_nozzle must differ")
        if self.analysis is not None and self.analysis.wall_thickness >= (
            self.tube.outer_diameter / 2
        ):
            raise ValueError(f"line {self.id}: wall thickness must be less than the tube radius")
        return self


class DesignParameters(StrictModel):
    """Design-wide routing parameters."""

    voxel_resolution: Annotated[float, Field(gt=0)]
    routing_cost_weights: RoutingWeights = Field(default_factory=RoutingWeights)


class RouteRecord(StrictModel):
    """Centreline of a routed line, stored once the deterministic pipeline produced it."""

    line: DesignId
    points: list[Point3] = Field(min_length=2)
    length_mm: Annotated[float, Field(ge=0)]


class InputHash(StrictModel):
    """Provenance of one input file the document was made from."""

    role: Literal["config", "step"]
    path: str = Field(min_length=1)
    sha256: Sha256


class DesignDocument(StrictModel):
    """Versioned canonical design (all lengths in millimetres)."""

    schema_version: Literal[1] = DESIGN_SCHEMA_VERSION
    title: str = Field(min_length=1)
    synthetic_data: Literal[True]
    units: Literal["mm"] = "mm"
    workspace: Workspace
    equipment: list[Equipment] = Field(default_factory=list)
    nozzles: list[Nozzle] = Field(min_length=2)
    lines: list[Line] = Field(min_length=1)
    parameters: DesignParameters
    routes: list[RouteRecord] = Field(default_factory=list)
    rationale: list[Rationale] = Field(default_factory=list)
    inputs: list[InputHash] = Field(default_factory=list)

    @model_validator(mode="after")
    def references_must_resolve(self) -> DesignDocument:
        _require_unique("equipment", [item.id for item in self.equipment])
        _require_unique("nozzle", [item.id for item in self.nozzles])
        _require_unique("line", [item.id for item in self.lines])
        _require_unique("route", [item.line for item in self.routes])
        equipment = {item.id for item in self.equipment}
        nozzles = {item.id for item in self.nozzles}
        lines = {item.id for item in self.lines}
        dims = self.workspace.dimensions
        for nozzle in self.nozzles:
            if nozzle.equipment is not None and nozzle.equipment not in equipment:
                raise ValueError(f"nozzle {nozzle.id}: unknown equipment {nozzle.equipment}")
            if any(v < 0 or v > limit for v, limit in zip(nozzle.port.position, dims, strict=True)):
                raise ValueError(f"nozzle {nozzle.id} lies outside the workspace")
        for line in self.lines:
            for end in (line.from_nozzle, line.to_nozzle):
                if end not in nozzles:
                    raise ValueError(f"line {line.id}: unknown nozzle {end}")
        for route in self.routes:
            if route.line not in lines:
                raise ValueError(f"route for unknown line {route.line}")
        paths = [record.path for record in self.rationale]
        _require_unique("rationale path", paths)
        if paths != sorted(paths):
            raise ValueError("rationale records must be sorted by path")
        for path in paths:
            root, _, rest = path.partition(".")
            line_id = rest.partition(".")[0]
            if root == "parameters" and rest:
                continue
            if root == "lines" and line_id in lines and rest.count(".") >= 1:
                continue
            raise ValueError(
                f"rationale path {path} must start with parameters. or lines.<line id>."
            )
        return self

    def line(self, line_id: str | None = None) -> Line:
        """The line ``line_id``, or the only line when ``line_id`` is ``None``."""
        if line_id is None:
            if len(self.lines) != 1:
                ids = [line.id for line in self.lines]
                raise DesignError(f"the design has {len(ids)} lines {ids}; name one")
            return self.lines[0]
        for line in self.lines:
            if line.id == line_id:
                return line
        raise DesignError(f"unknown line {line_id}")

    def nozzle(self, nozzle_id: str) -> Nozzle:
        for nozzle in self.nozzles:
            if nozzle.id == nozzle_id:
                return nozzle
        raise DesignError(f"unknown nozzle {nozzle_id}")


def _require_unique(kind: str, values: list[str]) -> None:
    duplicates = sorted({value for value in values if values.count(value) > 1})
    if duplicates:
        raise ValueError(f"duplicate {kind} ids: {duplicates}")


# ---- rationale paths ------------------------------------------------------------------


def design_path(config_path: str, line_id: str) -> str:
    """Design-document rationale path for a run-configuration path."""
    root = config_path.partition(".")[0]
    if root in _PARAMETER_ROOTS:
        return f"parameters.{config_path}"
    return f"lines.{line_id}.{config_path}"


def config_path(path: str, line_id: str) -> str | None:
    """Run-configuration path for a design path, or ``None`` if it is not for ``line_id``."""
    if path.startswith("parameters."):
        return path.removeprefix("parameters.")
    prefix = f"lines.{line_id}."
    if path.startswith(prefix):
        return path.removeprefix(prefix)
    return None


# ---- conversion -----------------------------------------------------------------------

START_NOZZLE = "start"
END_NOZZLE = "end"
MAIN_LINE = "main"


def design_from_config(
    config: DemoConfig,
    rationale: Iterable[Rationale] = (),
    *,
    sizing: LineSizingSpec | None = None,
    inputs: Iterable[InputHash] = (),
    line_id: str = MAIN_LINE,
) -> DesignDocument:
    """Design document for a single-line run configuration.

    ``rationale`` holds the records of a caller that chose values (for example the line
    sizing rules); they override the records derived from ``config`` at the same path, as
    in ``execute_pipeline``. Equipment ids are the obstacle names; the two ports become
    the free nozzles ``start`` and ``end``.
    """
    merged = build_report(config.title, config_rationale(config), rationale)
    records = sorted(
        (
            record.model_copy(update={"path": design_path(record.path, line_id)})
            for record in merged.entries
        ),
        key=lambda record: record.path,
    )
    return DesignDocument(
        title=config.title,
        synthetic_data=config.synthetic_data,
        workspace=config.workspace,
        equipment=[Equipment(id=item.name, geometry=item) for item in config.obstacles],
        nozzles=[
            Nozzle(id=START_NOZZLE, port=config.start_port),
            Nozzle(id=END_NOZZLE, port=config.end_port),
        ],
        lines=[
            Line(
                id=line_id,
                from_nozzle=START_NOZZLE,
                to_nozzle=END_NOZZLE,
                tube=config.tube,
                line_sizing=sizing,
                analysis=config.analysis,
            )
        ],
        parameters=DesignParameters(
            voxel_resolution=config.voxel_resolution,
            routing_cost_weights=config.routing_cost_weights,
        ),
        rationale=records,
        inputs=sorted(inputs, key=lambda item: (item.role, item.path)),
    )


def design_from_yaml(path: str | Path) -> DesignDocument:
    """Convert a YAML run configuration (fixed OD or ``line_sizing``) into a design.

    Records the SHA-256 of the YAML file and of every STEP file it references; a
    referenced STEP file that does not exist is refused rather than left unhashed.
    """
    source = Path(path)
    data = source.read_bytes()
    raw = yaml.safe_load(data)
    config, records = load_sized_config(source)
    sizing = None
    if isinstance(raw, dict) and raw.get(LINE_SIZING_KEY) is not None:
        sizing = LineSizingSpec.model_validate(raw[LINE_SIZING_KEY])
    inputs = [InputHash(role="config", path=source.name, sha256=_sha256(data))]
    for obstacle in config.obstacles:
        if isinstance(obstacle, StepObstacle):
            step = Path(obstacle.path)
            if not step.is_file():
                raise DesignError(f"equipment {obstacle.name}: STEP file {step} not found")
            inputs.append(InputHash(role="step", path=step.name, sha256=_sha256(step.read_bytes())))
    return design_from_config(config, records, sizing=sizing, inputs=inputs)


def run_config(design: DesignDocument, line_id: str | None = None) -> DemoConfig:
    """Run configuration for one line; every other line is not part of it.

    All equipment is included as obstacles. Routes of other lines are not yet obstacles
    (multi-line routing is roadmap milestone B1).
    """
    line = design.line(line_id)
    return DemoConfig(
        title=design.title,
        synthetic_data=design.synthetic_data,
        workspace=design.workspace,
        obstacles=[item.geometry for item in design.equipment],
        start_port=design.nozzle(line.from_nozzle).port,
        end_port=design.nozzle(line.to_nozzle).port,
        tube=line.tube,
        voxel_resolution=design.parameters.voxel_resolution,
        routing_cost_weights=design.parameters.routing_cost_weights,
        analysis=line.analysis,
    )


def run_rationale(design: DesignDocument, line_id: str | None = None) -> list[Rationale]:
    """Records for one line's run configuration, as ``execute_pipeline`` expects them."""
    line = design.line(line_id)
    records = []
    for record in design.rationale:
        path = config_path(record.path, line.id)
        if path is not None:
            records.append(record.model_copy(update={"path": path}))
    return sorted(records, key=lambda record: record.path)


# ---- persistence ----------------------------------------------------------------------

Migration = Callable[[dict[str, Any]], dict[str, Any]]
# MIGRATIONS[n] upgrades a raw version-n document to version n + 1.
MIGRATIONS: dict[int, Migration] = {}


def migrate(raw: dict[str, Any]) -> dict[str, Any]:
    """Upgrade a raw document to the current schema version, one step at a time.

    A document from a newer Syntera, or an old version with no migration, is refused.
    """
    version = raw.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise DesignError("design document has no integer schema_version")
    if version > DESIGN_SCHEMA_VERSION:
        raise DesignError(
            f"design schema_version {version} is newer than this Syntera supports "
            f"({DESIGN_SCHEMA_VERSION})"
        )
    data = dict(raw)
    while version < DESIGN_SCHEMA_VERSION:
        step = MIGRATIONS.get(version)
        if step is None:
            raise DesignError(f"no migration from design schema_version {version}")
        data = step(data)
        if data.get("schema_version") != version + 1:
            raise DesignError(f"migration from schema_version {version} did not upgrade it")
        version += 1
    return data


def parse_design(raw: Any) -> DesignDocument:
    """Validate a raw (JSON-decoded) design document, migrating it first."""
    if not isinstance(raw, dict):
        raise DesignError("a design document must be a JSON object")
    return DesignDocument.model_validate(migrate(raw))


def design_bytes(design: DesignDocument) -> bytes:
    """Canonical JSON encoding: identical designs give identical bytes."""
    text = json.dumps(design.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    return text.encode("utf-8")


def design_sha256(design: DesignDocument) -> str:
    return _sha256(design_bytes(design))


def save_design(design: DesignDocument, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(design_bytes(design))
    return target


def load_design(path: str | Path) -> DesignDocument:
    source = Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise DesignError(f"{source}: not valid JSON: {error}") from error
    return parse_design(raw)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
