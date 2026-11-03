"""Line isometrics and bill of materials from the design document (roadmap C3).

For every routed line of a design, fittings are placed exactly as the pipeline places
them (B2): elbows at bends and, for flanged lines, flanges at the ends. The bill of
materials counts fittings, lists every straight cut length and names the material, all
derived from the design document. Each line also gets a single-line isometric sheet with
corner-to-corner run lengths, fitting marks and its BOM table. Isometric entities carry
``SYNTERA`` references (C4) to their line.

The BOM is a quantity take-off for review, not a purchasing document; its fitting tables
are synthetic.
"""

from __future__ import annotations

import math
from collections import Counter
from pathlib import Path

import ezdxf
from ezdxf import units
from ezdxf.enums import TextEntityAlignment
from pydantic import BaseModel, ConfigDict

from syntera.components.fittings import FittingReport, place_fittings
from syntera.components.tables import SYNTHETIC_COMPONENT_NOTICE
from syntera.design import DesignDocument, Line
from syntera.drawings.trace import APPID, line_ref, tag
from syntera.geometry.centreline import centreline_segments
from syntera.schemas import FittingSpec, Point3

BOM_FILE = "bom.json"
ISO_PREFIX = "iso_"
COS30 = math.cos(math.radians(30))
SIN30 = 0.5
SHEET = (420.0, 297.0)  # A3 landscape
MARGIN = 10.0
TEXT = 3.0
WELDED = FittingSpec(end_connection="welded")

Point2 = tuple[float, float]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BomRow(StrictModel):
    line: str
    item: str
    description: str
    size: str
    material: str
    quantity: float
    unit: str
    lengths_mm: list[float] = []


class LineTakeOff(StrictModel):
    line: str
    material: str
    centreline_mm: float
    run_lengths_mm: list[float]
    fittings: FittingReport
    rows: list[BomRow]


class BillOfMaterials(StrictModel):
    title: str
    synthetic_data: bool
    tables: str = SYNTHETIC_COMPONENT_NOTICE
    lines: list[LineTakeOff]
    totals: list[BomRow]


def _material(line: Line) -> str:
    return line.analysis.material.name if line.analysis is not None else "unspecified"


def _size(line: Line) -> str:
    wall = f" x {line.analysis.wall_thickness:g}" if line.analysis is not None else ""
    return f"OD {line.tube.outer_diameter:g}{wall}"


def take_off(design: DesignDocument, line_id: str) -> LineTakeOff:
    """Fittings, cut lengths and BOM rows for one routed line."""
    line = design.line(line_id)
    route = next((r for r in design.routes if r.line == line_id), None)
    if route is None:
        raise ValueError(f"line {line_id} has no stored route; route the design first")
    points = list(route.points)
    start = design.nozzle(line.from_nozzle).port.direction
    end = design.nozzle(line.to_nozzle).port.direction
    placed = place_fittings(points, line.tube.outer_diameter, line.tube.minimum_bend_radius,
                            line.fittings or WELDED, line=line.id, start_direction=start,
                            end_direction=end)  # fmt: skip
    report = placed.report
    material = _material(line)
    size = _size(line)
    lengths = [piece.length_mm for piece in report.straight_pieces]
    rows = [
        BomRow(
            line=line.id,
            item="pipe",
            description="straight pipe, cut lengths",
            size=size,
            material=material,
            quantity=round(sum(lengths), 3),
            unit="mm",
            lengths_mm=lengths,
        )  # fmt: skip
    ]
    groups: Counter[tuple[str, str]] = Counter()
    for fitting in report.fittings:
        if fitting.kind == "flange":
            key = ("flange", f"OD {fitting.tube_od:g} {fitting.rating}")
        else:
            key = (fitting.kind, f"OD {fitting.tube_od:g} R {fitting.radius:g}")
        groups[key] += 1
    descriptions = {
        "elbow_lr": "90 deg long-radius elbow",
        "pipe_bend": "90 deg pipe bend (bent from the pipe)",
        "flange": "slip-on flange",
    }
    for (kind, fitting_size), count in sorted(groups.items()):
        rows.append(
            BomRow(
                line=line.id,
                item=kind,
                description=descriptions[kind],
                size=fitting_size,
                material=material,
                quantity=count,
                unit="ea",
            )  # fmt: skip
        )
    runs = [round(math.dist(a, b), 3) for a, b in zip(points, points[1:], strict=False)]
    centreline = sum(s.length for s in centreline_segments(points, line.tube.minimum_bend_radius))
    return LineTakeOff(
        line=line.id,
        material=material,
        centreline_mm=round(centreline, 6),
        run_lengths_mm=runs,
        fittings=report,
        rows=rows,
    )


def bill_of_materials(design: DesignDocument) -> BillOfMaterials:
    """Take-off of every routed line plus totals by item, size and material."""
    routed = sorted(route.line for route in design.routes)
    lines = [take_off(design, line_id) for line_id in routed]
    totals: dict[tuple[str, str, str, str], BomRow] = {}
    for takeoff in lines:
        for row in takeoff.rows:
            key = (row.item, row.size, row.material, row.unit)
            if key in totals:
                existing = totals[key]
                totals[key] = existing.model_copy(
                    update={
                        "quantity": round(existing.quantity + row.quantity, 3),
                        "lengths_mm": existing.lengths_mm + row.lengths_mm,
                    }
                )
            else:
                totals[key] = row.model_copy(update={"line": "all"})
    return BillOfMaterials(
        title=design.title,
        synthetic_data=design.synthetic_data,
        lines=lines,
        totals=[totals[key] for key in sorted(totals)],
    )


# ---- isometric sheet --------------------------------------------------------------------


def iso_point(point: Point3) -> Point2:
    """Standard isometric projection: model x to the lower right, y to the lower left."""
    x, y, z = point
    return ((x - y) * COS30, z - (x + y) * SIN30)


def write_isometric(design: DesignDocument, takeoff: LineTakeOff, path: Path) -> Path:
    route = next(r for r in design.routes if r.line == takeoff.line)
    flat = [iso_point(p) for p in route.points]
    xs, ys = [p[0] for p in flat], [p[1] for p in flat]
    width, height = max(xs) - min(xs) or 1.0, max(ys) - min(ys) or 1.0
    area = (SHEET[0] - 2 * MARGIN - 150, SHEET[1] - 2 * MARGIN - 40)
    scale = min(area[0] / width, area[1] / height)
    ox = MARGIN + 20 - min(xs) * scale
    oy = MARGIN + 30 - min(ys) * scale

    def at(point: Point2) -> Point2:
        return (ox + point[0] * scale, oy + point[1] * scale)

    doc = ezdxf.new("R2018", setup=True)
    doc.units = units.MM
    doc.appids.new(APPID)
    for name, colour in (("ISO-PIPE", 4), ("ISO-FITTING", 1), ("ISO-TEXT", 7), ("FRAME", 7)):
        doc.layers.add(name, color=colour)
    space = doc.modelspace()
    ref = line_ref(takeoff.line)

    def add(entity, reference: str = ref) -> None:
        tag(entity, reference)

    add(space.add_lwpolyline([(0, 0), (SHEET[0], 0), SHEET, (0, SHEET[1])], close=True,
                             dxfattribs={"layer": "FRAME"}), "drawing:frame")  # fmt: skip
    add(space.add_lwpolyline([at(p) for p in flat], dxfattribs={"layer": "ISO-PIPE"}))
    for (a, b), length in zip(zip(flat, flat[1:], strict=False), takeoff.run_lengths_mm,
                              strict=True):  # fmt: skip
        middle = at(((a[0] + b[0]) / 2, (a[1] + b[1]) / 2))
        text = space.add_text(f"{length:g}", height=TEXT, dxfattribs={"layer": "ISO-TEXT"})
        text.set_placement((middle[0] + 2, middle[1] + 2), align=TextEntityAlignment.LEFT)
        add(text)
    for fitting in takeoff.fittings.fittings:
        if fitting.kind == "flange":
            centre = at(iso_point(fitting.position))
            mark = space.add_circle(centre, 2.5, dxfattribs={"layer": "ISO-FITTING"})
        else:
            corner = min(route.points, key=lambda p: math.dist(p, fitting.position))
            mark = space.add_circle(at(iso_point(corner)), 1.5, dxfattribs={"layer": "ISO-FITTING"})
        add(mark)
    rows = takeoff.rows
    x0, y = SHEET[0] - MARGIN - 145, SHEET[1] - MARGIN - 8
    cut = ", ".join(f"{value:g}" for value in rows[0].lengths_mm)
    lines = [f"LINE {takeoff.line} - BILL OF MATERIALS"]
    lines += [
        f"{row.item}: {row.size} {row.material} x {row.quantity:g} {row.unit}" for row in rows
    ]
    lines += [
        f"cut lengths: {cut}",
        "run lengths are centreline corner to corner (mm)",
        "fitting tables: synthetic",
    ]
    for text_line in lines:
        text = space.add_text(text_line, height=TEXT * 0.8, dxfattribs={"layer": "ISO-TEXT"})
        text.set_placement((x0, y), align=TextEntityAlignment.LEFT)
        add(text)
        y -= 6
    title = space.add_text(f"{design.title} - ISOMETRIC LINE {takeoff.line}", height=TEXT,
                           dxfattribs={"layer": "ISO-TEXT"})  # fmt: skip
    title.set_placement((MARGIN + 2, MARGIN + 2), align=TextEntityAlignment.LEFT)
    add(title, "drawing:title-block")
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(path)
    return path
