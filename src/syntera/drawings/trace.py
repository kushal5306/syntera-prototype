"""Drawing-to-model traceability: every generated DXF entity names its design item (C4).

Generated sheets tag each entity with XDATA under the ``SYNTERA`` application id holding a
reference string:

- ``equipment:<id>``, ``nozzle:<id>`` and ``line:<id>`` name items of the design document;
- ``view:<name>`` names a projected view (overall dimensions and view labels);
- ``drawing:<part>`` names sheet furniture (frame, title block, notes).

:func:`resolve` turns a reference back into the design item, and :func:`trace_dxf` checks
that every entity in a generated DXF resolves.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import cadquery as cq
import ezdxf
from ezdxf.entities import DXFEntity
from pydantic import BaseModel, ConfigDict

from syntera.design import DesignDocument
from syntera.geometry.cad import _port_shape, make_tube, obstacle_shape

APPID = "SYNTERA"
DRAWING_PARTS = frozenset({"frame", "title-block", "note"})
VIEWS = frozenset({"plan", "front", "right", "isometric"})
DEFAULT_PORT_RADIUS_MM = 5.0

RefKind = Literal["equipment", "nozzle", "line", "view", "drawing"]


class Resolved(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: str
    kind: RefKind
    id: str


class TraceReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entities: int
    resolved: int
    by_kind: dict[str, int]
    untagged: list[str]
    unresolved: list[str]


def equipment_ref(item_id: str) -> str:
    return f"equipment:{item_id}"


def nozzle_ref(item_id: str) -> str:
    return f"nozzle:{item_id}"


def line_ref(item_id: str) -> str:
    return f"line:{item_id}"


def design_shapes(design: DesignDocument) -> list[tuple[str, cq.Shape]]:
    """Labelled exact shapes of a design: equipment, routed lines and nozzle markers."""
    shapes: list[tuple[str, cq.Shape]] = [
        (equipment_ref(item.id), obstacle_shape(item.geometry)) for item in design.equipment
    ]
    radius_of: dict[str, float] = {}
    for line in design.lines:
        for nozzle in (line.from_nozzle, line.to_nozzle):
            radius_of.setdefault(nozzle, line.tube.outer_diameter / 2)
    for route in sorted(design.routes, key=lambda record: record.line):
        tube = design.line(route.line).tube
        shape = make_tube(route.points, tube.outer_diameter / 2, tube.minimum_bend_radius)
        shapes.append((line_ref(route.line), shape))
    for nozzle in design.nozzles:
        radius = radius_of.get(nozzle.id, DEFAULT_PORT_RADIUS_MM)
        marker = _port_shape(nozzle.port.position, nozzle.port.direction, radius)
        shapes.append((nozzle_ref(nozzle.id), marker))
    return shapes


def tag(entity: DXFEntity, ref: str) -> None:
    entity.set_xdata(APPID, [(1000, ref)])


def ref_of(entity: DXFEntity) -> str | None:
    if not entity.has_xdata(APPID):
        return None
    values = [value for code, value in entity.get_xdata(APPID) if code == 1000]
    return str(values[0]) if values else None


def resolve(ref: str, design: DesignDocument) -> Resolved | None:
    """The design item (or view, or sheet part) a reference names, or ``None``."""
    kind, _, item = ref.partition(":")
    known: dict[str, set[str]] = {
        "equipment": {entry.id for entry in design.equipment},
        "nozzle": {entry.id for entry in design.nozzles},
        "line": {entry.id for entry in design.lines},
        "view": set(VIEWS),
        "drawing": set(DRAWING_PARTS),
    }
    if kind in known and item in known[kind]:
        return Resolved(ref=ref, kind=kind, id=item)  # type: ignore[arg-type]
    return None


def trace_dxf(path: Path, design: DesignDocument) -> TraceReport:
    """Resolve every model-space entity of a generated DXF against the design."""
    doc = ezdxf.readfile(path)
    untagged: list[str] = []
    unresolved: list[str] = []
    by_kind: dict[str, int] = {}
    entities = 0
    for entity in doc.modelspace():
        entities += 1
        ref = ref_of(entity)
        if ref is None:
            untagged.append(f"{entity.dxftype()} {entity.dxf.handle}")
            continue
        found = resolve(ref, design)
        if found is None:
            unresolved.append(f"{entity.dxftype()} {entity.dxf.handle}: {ref}")
            continue
        by_kind[found.kind] = by_kind.get(found.kind, 0) + 1
    return TraceReport(
        entities=entities,
        resolved=sum(by_kind.values()),
        by_kind=dict(sorted(by_kind.items())),
        untagged=untagged,
        unresolved=unresolved,
    )
