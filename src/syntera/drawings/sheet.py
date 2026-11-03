"""ISO drawing sheets from hidden-line views: frame, title block, scale and dimensions (C2).

A sheet takes the exact projection of a routed assembly (C1), picks an ISO A-size sheet
and an ISO 5455 scale by a fixed rule, places the views, and adds dimensions: overall size
of each orthographic view, nozzle positions from the model origin in plan, and the
corner-to-corner centreline run lengths of every route in plan. Dimension text shows true
model lengths (``DIMLFAC`` undoes the drawing scale).

The title block repeats the assurance verdict from the run's report and says that the
sheet is a review drawing, never evidence. ``sheet.json`` records every decision.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import ezdxf
from ezdxf import units
from ezdxf.enums import TextEntityAlignment
from ezdxf.layouts import Modelspace
from pydantic import BaseModel, ConfigDict

from syntera.design import DESIGN_FILE, design_sha256, load_design
from syntera.drawings.projection import (
    LAYER_HIDDEN,
    LAYER_LABEL,
    LAYER_VISIBLE,
    PROJECTION_FILE,
    ProjectedView,
    Projection,
    Vector3,
    project_shapes,
    step_solids,
    view_offsets,
)
from syntera.rationale import Rationale
from syntera.reporting.output import write_json

SHEET_FILE = "sheet.json"
SHEET_DXF = "sheet.dxf"
SHEET_RULE = "drawing.sheet_and_scale.v1"
# ISO 216 A-series, landscape (width, height) in mm, smallest first.
SHEET_SIZES: dict[str, tuple[float, float]] = {
    "A4": (297.0, 210.0),
    "A3": (420.0, 297.0),
    "A2": (594.0, 420.0),
    "A1": (841.0, 594.0),
    "A0": (1189.0, 841.0),
}
PREFERRED_SHEETS = ("A3", "A2", "A1", "A0")
# ISO 5455 scales as drawing:model ratios, largest first.
SCALES: tuple[tuple[int, int], ...] = (
    (5, 1), (2, 1), (1, 1), (1, 2), (1, 5), (1, 10), (1, 20), (1, 50), (1, 100), (1, 200),
    (1, 500), (1, 1000),
)  # fmt: skip
MIN_PREFERRED_SCALE = 1 / 10
BINDING_MARGIN_MM = 20.0
MARGIN_MM = 10.0
TITLE_BLOCK = (180.0, 56.0)
# room around the placed views for dimensions and labels (sheet mm)
DIMENSION_ROOM_MM = 30.0
TEXT_MM = 3.5
ORTHOGRAPHIC = ("plan", "front", "right")
LAYERS = {
    "FRAME": 7,
    "TITLE": 7,
    "DIMENSION": 2,
    "NOTE": 3,
}

Point2 = tuple[float, float]
Point3 = tuple[float, float, float]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SheetChoice(StrictModel):
    sheet: str
    width_mm: float
    height_mm: float
    scale: tuple[int, int]
    preferred: bool

    @property
    def factor(self) -> float:
        return self.scale[0] / self.scale[1]

    @property
    def label(self) -> str:
        return f"{self.scale[0]}:{self.scale[1]}"


class SheetDimension(StrictModel):
    view: str
    kind: Literal["overall", "nozzle", "run"]
    label: str
    model_length_mm: float
    start: Point2
    end: Point2
    angle: float


class SheetLabel(StrictModel):
    """A nozzle or route the dimensions refer to, with its model position."""

    id: str
    position: Point3


class SheetRoute(StrictModel):
    line: str
    points: list[Point3]


class SheetInput(StrictModel):
    title: str
    drawing_number: str
    verdict: bool | None
    verdict_source: str
    design_sha256: str | None
    synthetic_data: bool
    nozzles: list[SheetLabel]
    routes: list[SheetRoute]


class SheetLayout(StrictModel):
    """Every decision behind one sheet; identical inputs give an identical layout."""

    title: str
    drawing_number: str
    choice: SheetChoice
    view_origins: dict[str, Point2]
    dimensions: list[SheetDimension]
    title_block: dict[str, str]
    rationale: list[Rationale]


def drawing_area(sheet: str) -> tuple[float, float]:
    """Usable width and height inside the frame, above the title block strip."""
    width, height = SHEET_SIZES[sheet]
    return (
        width - BINDING_MARGIN_MM - MARGIN_MM,
        height - 2 * MARGIN_MM - TITLE_BLOCK[1],
    )


def arrangement_size(projection: Projection, factor: float) -> Point2:
    """Paper size of the placed views at ``factor``, with room for dimensions all round."""
    scaled = _scaled_projection(projection, factor)
    offsets = view_offsets(scaled)
    boxes = []
    for view in scaled.views:
        if view.bounds is not None:
            (x0, y0), (x1, y1) = view.bounds
            dx, dy = offsets[view.name]
            boxes.append((x0 + dx, y0 + dy, x1 + dx, y1 + dy))
    if not boxes:
        return (0.0, 0.0)
    width = max(b[2] for b in boxes) - min(b[0] for b in boxes)
    height = max(b[3] for b in boxes) - min(b[1] for b in boxes)
    return width + 2 * DIMENSION_ROOM_MM, height + 2 * DIMENSION_ROOM_MM


def choose_sheet(projection: Projection) -> SheetChoice:
    """Smallest preferred sheet at a scale no smaller than 1:10; else the largest fitting
    scale on the smallest sheet that fits at all."""
    fallback: SheetChoice | None = None
    for sheet in PREFERRED_SHEETS:
        area = drawing_area(sheet)
        for scale in SCALES:
            width, height = arrangement_size(projection, scale[0] / scale[1])
            if width > area[0] or height > area[1]:
                continue
            preferred = scale[0] / scale[1] >= MIN_PREFERRED_SCALE
            choice = SheetChoice(
                sheet=sheet,
                width_mm=SHEET_SIZES[sheet][0],
                height_mm=SHEET_SIZES[sheet][1],
                scale=scale,
                preferred=preferred,
            )
            if preferred:
                return choice
            if fallback is None:
                fallback = choice
            break
    if fallback is None:
        raise ValueError("the views do not fit an A0 sheet at 1:1000")
    return fallback


def view_sizes(projection: Projection) -> dict[str, Point2]:
    sizes = {}
    for view in projection.views:
        if view.bounds is not None:
            (x0, y0), (x1, y1) = view.bounds
            sizes[view.name] = (x1 - x0, y1 - y0)
    return sizes


def to_view(point: Point3, view: ProjectedView) -> Point2:
    """Model point to a view's plane coordinates (same frame as the HLR projection)."""
    x = _unit(view.x_direction)
    n = _unit(view.direction)
    y = (n[1] * x[2] - n[2] * x[1], n[2] * x[0] - n[0] * x[2], n[0] * x[1] - n[1] * x[0])
    return (sum(point[i] * x[i] for i in range(3)), sum(point[i] * y[i] for i in range(3)))


def _unit(vector: Vector3) -> Vector3:
    length = sum(v * v for v in vector) ** 0.5
    return tuple(v / length for v in vector)  # type: ignore[return-value]


def _scaled_projection(projection: Projection, factor: float) -> Projection:
    views = []
    for view in projection.views:
        bounds = None
        if view.bounds is not None:
            (x0, y0), (x1, y1) = view.bounds
            bounds = ((x0 * factor, y0 * factor), (x1 * factor, y1 * factor))
        views.append(view.model_copy(update={"bounds": bounds}))
    return projection.model_copy(update={"views": views})


def _origins(projection: Projection, choice: SheetChoice) -> dict[str, Point2]:
    """Where each view's model origin sits on the sheet (views centred in the area)."""
    factor = choice.factor
    scaled = _scaled_projection(projection, factor)
    offsets = view_offsets(scaled)
    boxes = []
    for view in scaled.views:
        if view.bounds is not None:
            (x0, y0), (x1, y1) = view.bounds
            dx, dy = offsets[view.name]
            boxes.append((x0 + dx, y0 + dy, x1 + dx, y1 + dy))
    if not boxes:
        return {}
    used = (min(b[0] for b in boxes), min(b[1] for b in boxes))
    extent = (max(b[2] for b in boxes) - used[0], max(b[3] for b in boxes) - used[1])
    area = drawing_area(choice.sheet)
    left = BINDING_MARGIN_MM + (area[0] - extent[0]) / 2 - used[0]
    bottom = MARGIN_MM + TITLE_BLOCK[1] + (area[1] - extent[1]) / 2 - used[1]
    return {
        name: (round(left + dx, 3) + 0.0, round(bottom + dy, 3) + 0.0)
        for name, (dx, dy) in offsets.items()
    }


def _dimensions(
    projection: Projection, sheet: SheetInput, choice: SheetChoice, origins: dict[str, Point2]
) -> list[SheetDimension]:
    factor = choice.factor
    dims: list[SheetDimension] = []

    def paper(view: ProjectedView, point: Point2) -> Point2:
        ox, oy = origins[view.name]
        return (round(ox + point[0] * factor, 3) + 0.0, round(oy + point[1] * factor, 3) + 0.0)

    for name in ORTHOGRAPHIC:
        try:
            view = projection.view(name)
        except KeyError:
            continue
        if view.bounds is None:
            continue
        (x0, y0), (x1, y1) = view.bounds
        dims.append(
            SheetDimension(view=name, kind="overall", label=f"{name} width",
                           model_length_mm=round(x1 - x0, 3),
                           start=paper(view, (x0, y0)), end=paper(view, (x1, y0)), angle=0.0)
        )  # fmt: skip
        dims.append(
            SheetDimension(view=name, kind="overall", label=f"{name} height",
                           model_length_mm=round(y1 - y0, 3),
                           start=paper(view, (x0, y0)), end=paper(view, (x0, y1)), angle=90.0)
        )  # fmt: skip
    try:
        plan = projection.view("plan")
    except KeyError:
        return dims
    for nozzle in sorted(sheet.nozzles, key=lambda item: item.id):
        x, y = to_view(nozzle.position, plan)
        for axis, (end, angle) in enumerate((((x, 0.0), 0.0), ((0.0, y), 90.0))):
            length = x if axis == 0 else y
            if abs(length) < 1e-6:
                continue
            dims.append(
                SheetDimension(
                    view="plan", kind="nozzle", label=f"nozzle {nozzle.id} {'xy'[axis]}",
                    model_length_mm=round(abs(length), 3),
                    start=paper(plan, (0.0, 0.0)),
                    end=paper(plan, end), angle=angle,
                )
            )  # fmt: skip
    for route in sorted(sheet.routes, key=lambda item: item.line):
        flat = [to_view(point, plan) for point in route.points]
        for index, (a, b) in enumerate(zip(flat, flat[1:], strict=False), start=1):
            dx, dy = b[0] - a[0], b[1] - a[1]
            if abs(dx) > 1e-6 and abs(dy) < 1e-6:
                angle, length = 0.0, abs(dx)
            elif abs(dy) > 1e-6 and abs(dx) < 1e-6:
                angle, length = 90.0, abs(dy)
            else:
                continue  # vertical in plan (seen end-on) or skewed: dimensioned elsewhere
            dims.append(
                SheetDimension(
                    view="plan", kind="run", label=f"line {route.line} run {index}",
                    model_length_mm=round(length, 3), start=paper(plan, a), end=paper(plan, b),
                    angle=angle,
                )
            )  # fmt: skip
    return dims


def _verdict_text(sheet: SheetInput) -> str:
    if sheet.verdict is None:
        return "NOT CHECKED"
    return "PASS" if sheet.verdict else "FAIL"


def title_block_fields(sheet: SheetInput, choice: SheetChoice) -> dict[str, str]:
    fields = {
        "TITLE": sheet.title,
        "DRAWING NO.": sheet.drawing_number,
        "SHEET": f"ISO {choice.sheet}",
        "SCALE": choice.label,
        "UNITS": "mm",
        "PROJECTION": "third angle",
        "ASSURANCE (EXACT GEOMETRY CHECKS)": _verdict_text(sheet),
        "VERDICT SOURCE": sheet.verdict_source,
        "STATUS": "REVIEW DRAWING - NOT EVIDENCE",
    }
    if sheet.design_sha256:
        fields["DESIGN SHA-256"] = sheet.design_sha256[:16]
    if sheet.synthetic_data:
        fields["DATA"] = "SYNTHETIC - NOT FOR CONSTRUCTION"
    return fields


def sheet_rationale(choice: SheetChoice, sizes: dict[str, Point2]) -> Rationale:
    return Rationale(
        path="drawing.sheet",
        value=f"{choice.sheet} {choice.label}",
        source="rule",
        rule_id=SHEET_RULE,
        inputs={
            "plan_mm": tuple(round(v, 3) for v in sizes.get("plan", (0.0, 0.0))),
            "front_mm": tuple(round(v, 3) for v in sizes.get("front", (0.0, 0.0))),
            "preferred": choice.preferred,
        },
        explanation=(
            "sheet and scale {value}: the smallest of A3, A2, A1, A0 on which the views fit "
            "at an ISO 5455 scale no smaller than 1:10, choosing the largest such scale "
            "(preferred: {preferred}); views of plan {plan_mm} mm and front {front_mm} mm."
        ),
    )


def layout_sheet(projection: Projection, sheet: SheetInput) -> SheetLayout:
    sizes = view_sizes(projection)
    choice = choose_sheet(projection)
    origins = _origins(projection, choice)
    return SheetLayout(
        title=sheet.title,
        drawing_number=sheet.drawing_number,
        choice=choice,
        view_origins=origins,
        dimensions=_dimensions(projection, sheet, choice, origins),
        title_block=title_block_fields(sheet, choice),
        rationale=[sheet_rationale(choice, sizes)],
    )


# ---- DXF --------------------------------------------------------------------------------


def _rectangle(space: Modelspace, low: Point2, high: Point2, layer: str) -> None:
    space.add_lwpolyline(
        [low, (high[0], low[1]), high, (low[0], high[1])],
        close=True,
        dxfattribs={"layer": layer},
    )


def _text(space: Modelspace, text: str, at: Point2, height: float, layer: str) -> None:
    space.add_text(text, height=height, dxfattribs={"layer": layer}).set_placement(
        at, align=TextEntityAlignment.LEFT
    )


def _title_block(space: Modelspace, layout: SheetLayout) -> None:
    width = layout.choice.width_mm
    right, bottom = width - MARGIN_MM, MARGIN_MM
    left, top = right - TITLE_BLOCK[0], bottom + TITLE_BLOCK[1]
    _rectangle(space, (left, bottom), (right, top), "TITLE")
    rows = list(layout.title_block.items())
    row_height = TITLE_BLOCK[1] / len(rows)
    for index, (key, value) in enumerate(rows):
        y = top - (index + 1) * row_height
        if index:
            space.add_line((left, y + row_height), (right, y + row_height),
                           dxfattribs={"layer": "TITLE"})  # fmt: skip
        _text(space, f"{key}: {value}", (left + 2, y + row_height * 0.25),
              min(TEXT_MM * 0.8, row_height * 0.6), "TITLE")  # fmt: skip


def write_sheet_dxf(projection: Projection, layout: SheetLayout, path: Path) -> Path:
    doc = ezdxf.new("R2018", setup=True)
    doc.units = units.MM
    for name, colour in LAYERS.items():
        doc.layers.add(name, color=colour)
    doc.layers.add(LAYER_VISIBLE, color=7)
    doc.layers.add(LAYER_HIDDEN, color=8, linetype="DASHED")
    doc.layers.add(LAYER_LABEL, color=2)
    style = doc.dimstyles.get("EZDXF")
    style.dxf.dimtxt = TEXT_MM * 0.7
    style.dxf.dimasz = TEXT_MM * 0.6
    style.dxf.dimexe = 1.0
    style.dxf.dimexo = 1.0
    style.dxf.dimdec = 0
    space = doc.modelspace()
    choice = layout.choice
    width, height = choice.width_mm, choice.height_mm
    _rectangle(space, (0.0, 0.0), (width, height), "FRAME")
    _rectangle(space, (BINDING_MARGIN_MM, MARGIN_MM), (width - MARGIN_MM, height - MARGIN_MM),
               "FRAME")  # fmt: skip
    factor = choice.factor
    for view in projection.views:
        ox, oy = layout.view_origins[view.name]
        for polyline in view.polylines:
            layer = LAYER_VISIBLE if polyline.visible else LAYER_HIDDEN
            space.add_lwpolyline(
                [(ox + x * factor, oy + y * factor) for x, y in polyline.points],
                dxfattribs={"layer": layer},
            )
        if view.bounds is not None:
            (x0, y0), _ = view.bounds
            _text(space, view.name.upper(), (ox + x0 * factor, oy + y0 * factor - 14),
                  TEXT_MM, LAYER_LABEL)  # fmt: skip
    for dim in layout.dimensions:
        offset = 8.0 if dim.kind == "overall" else 5.0
        if dim.angle == 0.0:
            base = ((dim.start[0] + dim.end[0]) / 2, min(dim.start[1], dim.end[1]) - offset)
        else:
            base = (min(dim.start[0], dim.end[0]) - offset, (dim.start[1] + dim.end[1]) / 2)
        space.add_linear_dim(
            base=base, p1=dim.start, p2=dim.end, angle=dim.angle, dimstyle="EZDXF",
            override={"dimlfac": 1 / factor}, dxfattribs={"layer": "DIMENSION"},
        ).render()  # fmt: skip
    _title_block(space, layout)
    _text(space, "Dimensions in mm; run lengths are centreline corner to corner.",
          (BINDING_MARGIN_MM + 2, MARGIN_MM + 2), TEXT_MM * 0.7, "NOTE")  # fmt: skip
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(path)
    return path


# ---- from a run folder ------------------------------------------------------------------

RUN_SOURCES = (
    ("routed_assembly.step", "assurance_report.json"),
    ("routed_design.step", "design_report.json"),
)


def sheet_from_run(run: Path, output: Path, drawing_number: str | None = None) -> SheetLayout:
    """Sheet for a ``demo`` or ``route-design`` output folder (needs its ``design.json``)."""

    design = load_design(run / DESIGN_FILE)
    found = [(step, report) for step, report in RUN_SOURCES if (run / step).is_file()]
    if not found:
        raise ValueError(f"{run}: no routed_assembly.step or routed_design.step to draw")
    step_name, report_name = found[0]
    verdict = None
    if (run / report_name).is_file():
        verdict = bool(json.loads((run / report_name).read_text("utf-8"))["overall_pass"])
    digest = design_sha256(design)
    sheet = SheetInput(
        title=design.title,
        drawing_number=drawing_number or f"SYN-{digest[:8].upper()}",
        verdict=verdict,
        verdict_source=report_name,
        design_sha256=digest,
        synthetic_data=design.synthetic_data,
        nozzles=[SheetLabel(id=n.id, position=n.port.position) for n in design.nozzles],
        routes=[SheetRoute(line=r.line, points=r.points) for r in design.routes],
    )
    projection = project_shapes(step_solids(run / step_name))
    layout = layout_sheet(projection, sheet)
    output.mkdir(parents=True, exist_ok=True)
    write_json(projection, output / PROJECTION_FILE)
    write_json(layout, output / SHEET_FILE)
    write_sheet_dxf(projection, layout, output / SHEET_DXF)
    return layout
