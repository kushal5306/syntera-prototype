"""Typed contracts for P&ID connectivity and the proposed 3D layout (millimetres)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Point3 = tuple[float, float, float]
Source = Literal["pid", "default"]


class PidModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PidNozzle(PidModel):
    id: str
    tag: str


class PidEquipment(PidModel):
    id: str
    tag: str
    component_class: str
    nozzles: list[PidNozzle]
    attributes: dict[str, float] = Field(default_factory=dict)


class Terminal(PidModel):
    """One end of a connection: an equipment nozzle or a battery limit (off-page/open end)."""

    id: str
    kind: Literal["nozzle", "battery_limit"]
    equipment_id: str | None = None
    label: str
    inbound: bool = False


class PidConnection(PidModel):
    """A pipe run Syntera must route: between two terminals of one P&ID line."""

    id: str
    line: str
    nominal_diameter: int | None
    start: Terminal
    end: Terminal
    via: list[str]
    note: str = ""


class PidDocument(PidModel):
    """Equipment and piping connectivity read from a DEXPI P&ID; positions are not used."""

    source_sha256: str
    filename: str
    originating_system: str | None
    equipment: list[PidEquipment]
    connections: list[PidConnection]
    skipped: list[str]


class PlacedEquipment(PidModel):
    tag: str
    component_class: str
    shape: Literal["box", "cylinder"]
    axis: Literal["x", "z"] = "z"
    center: Point3
    size: Point3
    size_source: Source
    size_note: str


class PlacedRoute(PidModel):
    connection_id: str
    line: str
    label: str
    outer_diameter_mm: float
    routed: bool
    assurance_pass: bool
    length_mm: float | None
    bends: int
    stub_length_mm: float
    estimated_cost: float | None
    minimum_clearance_mm: float | None
    failure_reasons: list[str]
    folder: str


class PlantLayoutReport(PidModel):
    """Proposed 3D layout satisfying a P&ID's connectivity; not a model of a real plant."""

    source_sha256: str
    filename: str
    workspace_mm: Point3
    footprint_m2: float
    voxel_resolution_mm: float
    currency: str
    equipment: list[PlacedEquipment]
    routes: list[PlacedRoute]
    routed: int
    failed: int
    total_pipe_length_mm: float
    total_bends: int
    total_cost: float
    layout_score_mm: float
    assumptions: list[str]
    skipped: list[str]
