"""Hidden-line projection of exact B-rep geometry into 2D views (roadmap C1).

OpenCascade's exact hidden-line removal (``HLRBRep_Algo``) projects the solids of a routed
assembly into plan, front, right-side and isometric views. Visible and hidden edges are
kept apart, discretised into polylines, rounded and sorted so the same geometry always
gives the same projection, and written to a DXF with visible and hidden edges on separate
layers. Each polyline records the solid it came from (its source label).

Drawings are review deliverables; they are never evidence for an assurance verdict.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import cadquery as cq
import ezdxf
from ezdxf import units
from ezdxf.enums import TextEntityAlignment
from OCP.BRepLib import BRepLib
from OCP.GCPnts import GCPnts_QuasiUniformDeflection
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
from OCP.HLRAlgo import HLRAlgo_Projector
from OCP.HLRBRep import HLRBRep_Algo, HLRBRep_HLRToShape
from pydantic import BaseModel, ConfigDict

PROJECTION_FILE = "projection.json"
VIEWS_DXF = "views.dxf"
DEFLECTION_MM = 0.05
BUILD_TOLERANCE = 1e-6
DIGITS = 4
VIEW_GAP_MM = 60.0
LAYER_VISIBLE = "HLR-VISIBLE"
LAYER_HIDDEN = "HLR-HIDDEN"
LAYER_LABEL = "HLR-LABEL"

Point2 = tuple[float, float]
Vector3 = tuple[float, float, float]
EdgeKind = Literal["sharp", "smooth", "outline"]


@dataclass(frozen=True)
class ViewSpec:
    """A view direction (pointing at the viewer) and the model direction drawn as sheet x."""

    name: str
    direction: Vector3
    x_direction: Vector3


STANDARD_VIEWS: tuple[ViewSpec, ...] = (
    ViewSpec("plan", (0.0, 0.0, 1.0), (1.0, 0.0, 0.0)),
    ViewSpec("front", (0.0, -1.0, 0.0), (1.0, 0.0, 0.0)),
    ViewSpec("right", (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
    ViewSpec("isometric", (1.0, -1.0, 1.0), (1.0, 1.0, 0.0)),
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Polyline(StrictModel):
    source: str
    visible: bool
    edge: EdgeKind
    points: list[Point2]


class ProjectedView(StrictModel):
    name: str
    direction: Vector3
    x_direction: Vector3
    bounds: tuple[Point2, Point2] | None
    polylines: list[Polyline]

    def count(self, visible: bool) -> int:
        return sum(polyline.visible is visible for polyline in self.polylines)


class Projection(StrictModel):
    """Every view of one assembly; coordinates are millimetres in each view's plane."""

    sources: list[str]
    deflection_mm: float
    views: list[ProjectedView]

    def view(self, name: str) -> ProjectedView:
        for view in self.views:
            if view.name == name:
                return view
        raise KeyError(name)


def _round(value: float) -> float:
    return round(value, DIGITS) + 0.0


def _unit(vector: Vector3) -> Vector3:
    length = math.sqrt(sum(v * v for v in vector))
    return tuple(v / length for v in vector)  # type: ignore[return-value]


def _edge_points(edge: cq.Edge) -> list[Point2]:
    curve = edge._geomAdaptor()
    points = GCPnts_QuasiUniformDeflection(
        curve, DEFLECTION_MM, curve.FirstParameter(), curve.LastParameter()
    )
    if not points.IsDone() or points.NbPoints() < 2:
        start, end = edge.startPoint(), edge.endPoint()
        raw = [(start.x, start.y), (end.x, end.y)]
    else:
        raw = []
        for index in range(1, points.NbPoints() + 1):
            point = points.Value(index)
            raw.append((point.X(), point.Y()))
    rounded = [(_round(x), _round(y)) for x, y in raw]
    cleaned = [rounded[0]]
    for point in rounded[1:]:
        if point != cleaned[-1]:
            cleaned.append(point)
    return cleaned


def _canonical(points: list[Point2]) -> list[Point2]:
    """One direction for every polyline so ordering never depends on edge orientation."""
    return points if points <= points[::-1] else points[::-1]


def _compound_polylines(
    compound: object, source: str, visible: bool, edge_kind: EdgeKind
) -> list[Polyline]:
    if compound is None or compound.IsNull():  # type: ignore[attr-defined]
        return []
    BRepLib.BuildCurves3d_s(compound, BUILD_TOLERANCE)
    shape = cq.Shape.cast(compound)  # type: ignore[arg-type]
    result = []
    for edge in shape.Edges():
        points = _edge_points(edge)
        if len(points) >= 2:
            result.append(
                Polyline(source=source, visible=visible, edge=edge_kind, points=_canonical(points))
            )
    return result


def project_view(shapes: Sequence[tuple[str, cq.Shape]], spec: ViewSpec) -> ProjectedView:
    """Exact hidden-line projection of labelled shapes into one view."""
    algo = HLRBRep_Algo()
    for _, shape in shapes:
        algo.Add(shape.wrapped)
    frame = gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(*_unit(spec.direction)))
    frame.SetXDirection(gp_Dir(*_unit(spec.x_direction)))
    algo.Projector(HLRAlgo_Projector(frame))
    algo.Update()
    algo.Hide()
    extract = HLRBRep_HLRToShape(algo)
    polylines: list[Polyline] = []
    for label, shape in shapes:
        wrapped = shape.wrapped
        polylines += _compound_polylines(extract.VCompound(wrapped), label, True, "sharp")
        polylines += _compound_polylines(extract.Rg1LineVCompound(wrapped), label, True, "smooth")
        polylines += _compound_polylines(extract.OutLineVCompound(wrapped), label, True, "outline")
        polylines += _compound_polylines(extract.HCompound(wrapped), label, False, "sharp")
        polylines += _compound_polylines(extract.OutLineHCompound(wrapped), label, False, "outline")
    polylines.sort(key=lambda p: (p.source, not p.visible, p.edge, p.points))
    xs = [x for polyline in polylines for x, _ in polyline.points]
    ys = [y for polyline in polylines for _, y in polyline.points]
    bounds = ((min(xs), min(ys)), (max(xs), max(ys))) if xs else None
    return ProjectedView(
        name=spec.name,
        direction=spec.direction,
        x_direction=spec.x_direction,
        bounds=bounds,
        polylines=polylines,
    )


def project_shapes(
    shapes: Sequence[tuple[str, cq.Shape]], views: Iterable[ViewSpec] = STANDARD_VIEWS
) -> Projection:
    """Project labelled shapes into every view. Labels must be unique."""
    labels = [label for label, _ in shapes]
    if len(set(labels)) != len(labels):
        raise ValueError(f"shape labels must be unique: {labels}")
    if not shapes:
        raise ValueError("nothing to project")
    return Projection(
        sources=labels,
        deflection_mm=DEFLECTION_MM,
        views=[project_view(shapes, spec) for spec in views],
    )


def step_solids(path: Path) -> list[tuple[str, cq.Shape]]:
    """Solids of a STEP file, labelled ``solid-1``, ``solid-2``... in file order."""
    imported = cq.importers.importStep(str(path))
    solids = [solid for value in imported.vals() for solid in value.Solids()]
    if not solids:
        raise ValueError(f"{path}: no solids to project")
    return [(f"solid-{index}", solid) for index, solid in enumerate(solids, start=1)]


# ---- DXF --------------------------------------------------------------------------------


def view_offsets(projection: Projection) -> dict[str, Point2]:
    """Sheet offsets: third-angle plan above front, right beside front, isometric top right."""
    size = {}
    for view in projection.views:
        if view.bounds is None:
            size[view.name] = (0.0, 0.0)
        else:
            (x0, y0), (x1, y1) = view.bounds
            size[view.name] = (x1 - x0, y1 - y0)
    front_w, front_h = size.get("front", (0.0, 0.0))
    right_w = size.get("right", (0.0, 0.0))[0]
    anchors = {
        "front": (0.0, 0.0),
        "plan": (0.0, front_h + VIEW_GAP_MM),
        "right": (front_w + VIEW_GAP_MM, 0.0),
        "isometric": (front_w + VIEW_GAP_MM, front_h + VIEW_GAP_MM),
    }
    offsets = {}
    column = front_w + right_w + 2 * VIEW_GAP_MM
    extra = 0.0
    for view in projection.views:
        anchor = anchors.get(view.name)
        if anchor is None:  # any further view goes in a column to the right
            anchor = (column, extra)
            extra += size[view.name][1] + VIEW_GAP_MM
        low = view.bounds[0] if view.bounds else (0.0, 0.0)
        offsets[view.name] = (anchor[0] - low[0], anchor[1] - low[1])
    return offsets


def write_projection_dxf(projection: Projection, path: Path) -> Path:
    """Write every view to one DXF: visible and hidden edges on separate layers."""
    doc = ezdxf.new("R2018", setup=True)
    doc.units = units.MM
    doc.layers.add(LAYER_VISIBLE, color=7)
    doc.layers.add(LAYER_HIDDEN, color=8, linetype="DASHED")
    doc.layers.add(LAYER_LABEL, color=2)
    space = doc.modelspace()
    offsets = view_offsets(projection)
    for view in projection.views:
        dx, dy = offsets[view.name]
        for polyline in view.polylines:
            layer = LAYER_VISIBLE if polyline.visible else LAYER_HIDDEN
            space.add_lwpolyline(
                [(x + dx, y + dy) for x, y in polyline.points], dxfattribs={"layer": layer}
            )
        low = view.bounds[0] if view.bounds else (0.0, 0.0)
        space.add_text(
            view.name.upper(), height=12, dxfattribs={"layer": LAYER_LABEL}
        ).set_placement((low[0] + dx, low[1] + dy - 25), align=TextEntityAlignment.LEFT)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(path)
    return path
