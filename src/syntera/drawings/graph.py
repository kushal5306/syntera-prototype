"""Semantic drawing graph: geometry, labels, dimensions and title-block facts (D3).

A read drawing becomes ``drawing_graph.json``:

- **nodes**: drawn geometry (lines, polylines, circles, arcs, plain block inserts) with
  layer, kind, bounding box and, for Syntera's own sheets, the design reference from
  the entity's ``SYNTERA`` XDATA;
- **labels**: TEXT, MTEXT and attributed inserts, each attached to the nearest geometry
  (inside a closed outline counts as distance zero; the smallest such outline wins);
- **dimensions**: each linked to the geometry its definition points touch, or, on
  Syntera sheets, to the geometry with the same design reference;
- **title block**: attribute values of title-block inserts.

Every association is a fixed geometric rule with handle tie-breaks, so the graph is
deterministic. Labels are associations by proximity, never facts about the design.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Literal

from ezdxf import bbox
from ezdxf.document import Drawing
from ezdxf.entities import DXFEntity
from ezdxf.layouts import BaseLayout
from pydantic import BaseModel, ConfigDict, Field

from syntera.drawings.reader import DIMENSION_KINDS, _measurement, load_dxf
from syntera.drawings.structure import block_role, normalise_mtext
from syntera.drawings.trace import ref_of

GRAPH_FILE = "drawing_graph.json"
GRAPH_VERSION = 1
GEOMETRY_TYPES = frozenset({"LINE", "LWPOLYLINE", "POLYLINE", "CIRCLE", "ARC", "INSERT"})
TAG_PATTERN = re.compile(r"\b[A-Z]{1,4}-?\d{2,5}[A-Z]?\b")
LABEL_REACH_HEIGHTS = 5.0  # a label attaches to geometry within 5 text heights
DIMENSION_TOLERANCE = 1e-3  # definition point to geometry, as a share of the extents
EQUIPMENT_LAYER = re.compile(r"EQUIP|VESSEL|PUMP|TANK|SKID", re.IGNORECASE)
PIPE_LAYER = re.compile(r"PIPE|PIPING|LINE", re.IGNORECASE)

NodeKind = Literal["equipment", "pipe", "nozzle", "line", "frame", "geometry"]
Point2 = tuple[float, float]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GraphNode(StrictModel):
    handle: str
    type: str
    layer: str
    kind: NodeKind
    ref: str | None = None
    bbox: tuple[Point2, Point2]
    tags: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)


class GraphLabel(StrictModel):
    handle: str
    text: str
    position: Point2
    height: float
    attached_to: str | None
    distance: float | None


class GraphDimension(StrictModel):
    handle: str
    kind: str
    measurement: float | None
    text: str
    points: list[Point2]
    measures: list[str]
    ref: str | None = None


class DrawingGraph(StrictModel):
    version: int = GRAPH_VERSION
    space: str
    nodes: list[GraphNode]
    labels: list[GraphLabel]
    dimensions: list[GraphDimension]
    title_block: dict[str, str]

    def node(self, handle: str) -> GraphNode:
        for node in self.nodes:
            if node.handle == handle:
                return node
        raise KeyError(handle)


# ---- geometry helpers -------------------------------------------------------------------


def _round(value: float) -> float:
    return round(value, 6) + 0.0


def _point(vector) -> Point2:
    return (_round(vector.x), _round(vector.y))


def _segment_distance(p: Point2, a: Point2, b: Point2) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = dx * dx + dy * dy
    t = (
        0.0
        if length == 0
        else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length))
    )
    return math.dist(p, (a[0] + t * dx, a[1] + t * dy))


def _inside(p: Point2, polygon: list[Point2]) -> bool:
    inside = False
    for (x1, y1), (x2, y2) in zip(polygon, polygon[1:] + polygon[:1], strict=False):
        if (y1 > p[1]) != (y2 > p[1]):
            x = x1 + (p[1] - y1) * (x2 - x1) / (y2 - y1)
            if p[0] < x:
                inside = not inside
    return inside


class _Shape:
    """Distance and area of one geometry entity, for association rules."""

    def __init__(self, entity: DXFEntity, box: tuple[Point2, Point2]):
        self.entity = entity
        self.box = box
        kind = entity.dxftype()
        self.segments: list[tuple[Point2, Point2]] = []
        self.polygon: list[Point2] | None = None
        self.circle: tuple[Point2, float] | None = None
        if kind == "LINE":
            self.segments = [(_point(entity.dxf.start), _point(entity.dxf.end))]
        elif kind == "LWPOLYLINE":
            points = [(_round(x), _round(y)) for x, y in entity.get_points("xy")]
            self.segments = list(zip(points, points[1:], strict=False))
            if entity.closed and len(points) >= 3:
                self.segments.append((points[-1], points[0]))
                self.polygon = points
        elif kind == "POLYLINE" and not entity.is_3d_polyline:
            points = [_point(vertex.dxf.location) for vertex in entity.vertices]
            self.segments = list(zip(points, points[1:], strict=False))
            if entity.is_closed and len(points) >= 3:
                self.segments.append((points[-1], points[0]))
                self.polygon = points
        elif kind in {"CIRCLE", "ARC"}:
            self.circle = (_point(entity.dxf.center), float(entity.dxf.radius))

    @property
    def area(self) -> float:
        if self.circle is not None and self.entity.dxftype() == "CIRCLE":
            return math.pi * self.circle[1] ** 2
        if self.polygon is not None:
            total = 0.0
            for (x1, y1), (x2, y2) in zip(
                self.polygon, self.polygon[1:] + self.polygon[:1], strict=False
            ):
                total += x1 * y2 - x2 * y1
            return abs(total) / 2
        return 0.0

    def distance(self, p: Point2, filled: bool) -> float:
        """Distance from ``p``; with ``filled``, a point inside a closed shape is at zero."""
        if self.circle is not None:
            centre, radius = self.circle
            gap = math.dist(p, centre) - radius
            if filled and self.entity.dxftype() == "CIRCLE" and gap <= 0:
                return 0.0
            return abs(gap)
        if self.segments:
            if filled and self.polygon is not None and _inside(p, self.polygon):
                return 0.0
            return min(_segment_distance(p, a, b) for a, b in self.segments)
        (x0, y0), (x1, y1) = self.box
        dx = max(x0 - p[0], 0.0, p[0] - x1)
        dy = max(y0 - p[1], 0.0, p[1] - y1)
        return math.hypot(dx, dy)


def _kind(entity: DXFEntity, ref: str | None) -> NodeKind:
    if ref is not None:
        prefix = ref.partition(":")[0]
        if prefix in {"equipment", "nozzle", "line"}:
            return prefix  # type: ignore[return-value]
        if prefix == "drawing":
            return "frame"
        return "geometry"
    layer = entity.dxf.layer
    if EQUIPMENT_LAYER.search(layer):
        return "equipment"
    if PIPE_LAYER.search(layer):
        return "pipe"
    return "geometry"


def _space(doc: Drawing) -> BaseLayout:
    model = doc.modelspace()
    if len(model):
        return model
    for name in doc.layouts.names_in_taborder():
        layout = doc.layouts.get(name)
        if name != "Model" and len(layout):
            return layout
    return model


def _definition_points(entity: DXFEntity) -> list[Point2]:
    points = []
    for name in ("defpoint2", "defpoint3", "defpoint4", "defpoint5"):
        if entity.dxf.hasattr(name):
            points.append(_point(entity.dxf.get(name)))
    return points


# ---- building ----------------------------------------------------------------------------


def build_graph(doc: Drawing) -> DrawingGraph:
    space = _space(doc)
    cache = bbox.Cache()
    shapes: list[_Shape] = []
    nodes: dict[str, GraphNode] = {}
    texts: list[tuple[DXFEntity, str, Point2, float]] = []
    dimension_entities: list[DXFEntity] = []
    title_block: dict[str, str] = {}
    corners: list[Point2] = []
    for entity in space:
        kind = entity.dxftype()
        if kind in {"TEXT", "MTEXT"}:
            text = normalise_mtext(entity) if kind == "MTEXT" else entity.dxf.text.strip()
            height = float(entity.dxf.get("char_height" if kind == "MTEXT" else "height", 2.5))
            if text:
                texts.append((entity, text, _point(entity.dxf.insert), height))
            continue
        if kind == "DIMENSION":
            dimension_entities.append(entity)
            continue
        if kind == "INSERT" and entity.attribs:
            values = {a.dxf.tag: a.dxf.text.strip() for a in entity.attribs}
            if block_role(list(values)) == "title_block":
                title_block.update(dict(sorted(values.items())))
                continue
            height = min((float(a.dxf.get("height", 2.5)) for a in entity.attribs), default=2.5)
            text = " ".join(value for _, value in sorted(values.items()) if value)
            texts.append((entity, text, _point(entity.dxf.insert), height))
            continue
        if kind not in GEOMETRY_TYPES:
            continue
        box = bbox.extents([entity], fast=False, cache=cache)
        if not box.has_data:
            continue
        low, high = _point(box.extmin), _point(box.extmax)
        corners += [low, high]
        ref = ref_of(entity)
        node = GraphNode(
            handle=entity.dxf.handle,
            type=kind,
            layer=entity.dxf.layer,
            kind=_kind(entity, ref),
            ref=ref,
            bbox=(low, high),
            tags=[ref.partition(":")[2]] if ref and ref.partition(":")[0] != "drawing" else [],
        )
        nodes[node.handle] = node
        shapes.append(_Shape(entity, (low, high)))

    extent = 0.0
    if corners:
        xs, ys = [c[0] for c in corners], [c[1] for c in corners]
        extent = math.hypot(max(xs) - min(xs), max(ys) - min(ys))

    labels = []
    for entity, text, position, height in texts:
        reach = LABEL_REACH_HEIGHTS * max(height, 1e-9)
        best: tuple[float, float, str] | None = None
        for shape in shapes:
            if nodes[shape.entity.dxf.handle].kind == "frame":
                continue
            distance = shape.distance(position, filled=True)
            if distance > reach:
                continue
            key = (_round(distance), shape.area, shape.entity.dxf.handle)
            if best is None or key < best:
                best = key
        attached = best[2] if best else None
        labels.append(
            GraphLabel(
                handle=entity.dxf.handle,
                text=text,
                position=position,
                height=_round(height),
                attached_to=attached,
                distance=best[0] if best else None,
            )
        )
        if attached:
            node = nodes[attached]
            node.labels.append(entity.dxf.handle)
            for tag in TAG_PATTERN.findall(text.upper()):
                if tag not in node.tags:
                    node.tags.append(tag)

    dimensions = []
    tolerance = max(DIMENSION_TOLERANCE * extent, 1e-6)
    for entity in dimension_entities:
        ref = ref_of(entity)
        points = _definition_points(entity)
        if ref is not None:
            measures = sorted(handle for handle, node in nodes.items() if node.ref == ref)
        else:
            touched = set()
            for point in points:
                for shape in shapes:
                    if shape.distance(point, filled=False) <= tolerance:
                        touched.add(shape.entity.dxf.handle)
            measures = sorted(touched)
        dimensions.append(
            GraphDimension(
                handle=entity.dxf.handle,
                kind=DIMENSION_KINDS.get(entity.dimtype & 7, "other"),
                measurement=_measurement(entity),
                text=entity.dxf.get("text", ""),
                points=points,
                measures=measures,
                ref=ref,
            )
        )
        for handle in measures:
            nodes[handle].dimensions.append(entity.dxf.handle)

    for node in nodes.values():
        node.tags.sort()
    return DrawingGraph(
        space=space.name,
        nodes=list(nodes.values()),
        labels=labels,
        dimensions=dimensions,
        title_block=title_block,
    )


def graph_from_file(path: Path) -> DrawingGraph:
    doc, _, _ = load_dxf(path)
    return build_graph(doc)


# ---- queries -----------------------------------------------------------------------------


def find_by_tag(graph: DrawingGraph, tag: str) -> list[GraphNode]:
    """Nodes tagged ``tag`` (case-insensitive): from labels, or a Syntera design reference."""
    wanted = tag.strip().upper()
    return [node for node in graph.nodes if wanted in {t.upper() for t in node.tags}]


def label_of(graph: DrawingGraph, handle: str) -> list[str]:
    """Texts of the labels attached to the entity ``handle``, in drawing order."""
    node = graph.node(handle)
    by_handle = {label.handle: label for label in graph.labels}
    return [by_handle[h].text for h in node.labels]


def dimensions_of(graph: DrawingGraph, handle: str) -> list[GraphDimension]:
    """Dimensions that measure the entity ``handle``."""
    node = graph.node(handle)
    by_handle = {dimension.handle: dimension for dimension in graph.dimensions}
    return [by_handle[h] for h in node.dimensions]
