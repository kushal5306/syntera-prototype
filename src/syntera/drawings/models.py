"""Typed contracts for imported 2D drawings."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from syntera.verification.assembly_audit import AssemblyAudit


class DrawingModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConversionRecord(DrawingModel):
    """Which external tool turned the DWG into DXF, for traceability."""

    converter: Literal["oda", "libredwg"]
    converter_version: str | None
    warnings: list[str]


class DrawingUnits(DrawingModel):
    code: int
    name: str
    mm_per_unit: float | None


class LayerSummary(DrawingModel):
    name: str
    color: int
    visible: bool
    frozen: bool
    entity_count: int


class DimensionNote(DrawingModel):
    handle: str
    layer: str
    kind: str
    measurement: float | None
    text: str


class TextNote(DrawingModel):
    handle: str
    layer: str
    text: str
    position: tuple[float, float]


class DrawingSummary(DrawingModel):
    """Deterministic facts read from the drawing's model space (or, if that draws nothing,
    its first paper-space layout), in drawing units."""

    drawing_id: str
    filename: str
    source_format: Literal["dwg", "dxf"]
    source_sha256: str
    dwg_version: str | None
    dxf_version: str
    conversion: ConversionRecord | None
    units: DrawingUnits
    extents: tuple[tuple[float, float], tuple[float, float]] | None
    view_extents: tuple[tuple[float, float], tuple[float, float]] | None
    svg_page_mm: tuple[float, float]
    clipped_handles: list[str]
    entity_count: int
    entity_types: dict[str, int]
    layers: list[LayerSummary]
    block_inserts: dict[str, int]
    dimensions: list[DimensionNote]
    texts: list[TextNote]
    truncated: bool
    audit_errors: int
    audit_fixes: int
    # Records written before render versions existed are version 1 (dark background).
    render_version: int = 1
    # Which layout the summary and SVG describe: "Model", or a paper-space layout name.
    space: str = "Model"


LayerRole = Literal[
    "equipment", "pipe", "pipe_centreline", "nozzle", "structure", "frame", "ignore"
]


class AssemblyOptions(DrawingModel):
    """User-editable inputs for turning a plan drawing into a 3D assembly (millimetres).

    ``None`` means "read it from the drawing, else use the stated default".
    """

    equipment_height_mm: float | None = Field(default=None, gt=0)
    pipe_elevation_mm: float | None = Field(default=None, gt=0)
    outer_diameter_mm: float | None = Field(default=None, gt=0)
    bend_radius_mm: float | None = Field(default=None, gt=0)
    clearance_mm: float | None = Field(default=None, ge=0)
    cost_per_metre: float = Field(default=120.0, ge=0)
    cost_per_bend: float = Field(default=45.0, ge=0)
    currency: str = Field(default="USD", min_length=1, max_length=8)
    layer_roles: dict[str, LayerRole] = Field(default_factory=dict)
    # False checks the drawing as drawn only (fast); True also routes each line to compare.
    optimise: bool = True


class Assumption(DrawingModel):
    """Where one value of the 3D model came from, so a reviewer can challenge it."""

    quantity: str
    value: str
    source: Literal["drawing", "default", "user"]
    note: str = ""


class InferredEquipment(DrawingModel):
    name: str
    shape: Literal["box", "cylinder"]
    layer: str
    plan_handle: str
    height_source: Literal["drawing", "default", "user"]


class RouteCheck(DrawingModel):
    """One route (as drawn, or Syntera's) measured and checked against exact geometry."""

    points: list[tuple[float, float, float]]
    length_mm: float | None
    straight_length_mm: float | None
    bend_length_mm: float | None
    bends: int
    estimated_cost: float | None
    buildable: bool
    assurance_pass: bool
    collision_free: bool | None
    minimum_clearance_mm: float | None
    failure_reasons: list[str]
    too_close_to: list[str] = Field(default_factory=list)


class LineCheck(DrawingModel):
    name: str
    folder: str
    line_number: str | None = None
    outer_diameter_mm: float | None = None
    # what each end connects to, e.g. "nozzle on T-201", "tee", "battery limit"
    ends: list[str] = Field(default_factory=list)
    nozzle_shift_mm: float
    voxel_resolution_mm: float
    drawn: RouteCheck
    optimised: RouteCheck
    length_saving_mm: float | None
    length_saving_percent: float | None
    bend_saving: int | None
    cost_saving: float | None
    findings: list[str]


class PipingCheckReport(DrawingModel):
    """Deterministic 3D reconstruction and piping review of one drawing as drawn."""

    drawing_id: str
    filename: str
    source_sha256: str
    supported: bool
    message: str
    currency: str
    workspace_mm: tuple[float, float, float] | None
    views_found: list[str]
    equipment: list[InferredEquipment]
    assumptions: list[Assumption]
    lines: list[LineCheck]
    total_drawn_length_mm: float | None
    total_optimised_length_mm: float | None
    total_cost_saving: float | None
    findings: list[str]
    # every drawn pipe, nozzle, item of equipment and structure checked against each other
    audit: AssemblyAudit | None = None
