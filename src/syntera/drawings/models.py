"""Typed contracts for imported 2D drawings."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


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
    """Deterministic facts read from a drawing's model space, in drawing units."""

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
