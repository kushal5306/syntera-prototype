"""Infer a 3D equipment-and-piping assembly from a plan-view layout drawing.

A plan fixes equipment footprints and pipe paths in X and Y only. Heights come from a front
elevation when the drawing has one, otherwise from stated defaults the user can edit. Every
value is recorded with its source so a reviewer can see what was read and what was assumed.

The inference is deterministic: entities are visited in a fixed order and every tie is
broken by coordinates, so the same drawing and options always give the same assembly.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

from ezdxf import bbox
from ezdxf.document import Drawing
from ezdxf.entities import DXFGraphic
from ezdxf.path import make_path

from syntera.drawings.annotations import Elevation, LineNumber, parse_elevation, parse_line_number
from syntera.drawings.models import (
    AssemblyOptions,
    Assumption,
    InferredEquipment,
    LayerRole,
)
from syntera.rationale import Rationale
from syntera.schemas import BoxObstacle, CylinderObstacle, Point3, TubeSpecification

Point2 = tuple[float, float]
Rect = tuple[float, float, float, float]  # x0, y0, x1, y1

DEFAULT_EQUIPMENT_HEIGHT_MM = 1000.0
DEFAULT_PIPE_ELEVATION_MM = 500.0
DEFAULT_OUTER_DIAMETER_MM = 60.3  # DN50
MAX_LINES = 40
ORIGIN_STEP_MM = 100.0
# Flange face of a nozzle stands this far off the equipment surface where the drawing does
# not say (a typical DN25-DN150 nozzle projection).
NOZZLE_PROJECTION_MM = 150.0
# How far a line number or elevation label may sit from the run it describes.
LABEL_REACH_MM = 1000.0
# A run ending this close to the room or skid limit is taken to leave it (battery limit).
BATTERY_LIMIT_REACH_MM = 500.0
# Equipment tags such as T-201, P-201A, E220: preferred over other text inside an outline.
EQUIPMENT_TAG = re.compile(r"^[A-Z]{1,4}-?\d{2,5}[A-Z]?$")

# Checked in this order; the first matching word decides the layer's role.
ROLE_WORDS: tuple[tuple[LayerRole, tuple[str, ...]], ...] = (
    ("pipe_centreline", ("PIPE-CL", "PIPE_CL", "PIPECL", "CENTRELINE", "CENTERLINE")),
    ("nozzle", ("NOZZLE", "NOZ", "PORT", "CONNECTION")),
    ("pipe", ("PIPE", "PIPING", "TUBE")),
    # EQPM is the US National CAD Standard equipment code (M-EQPM, P-EQPM).
    (
        "equipment",
        ("EQUIP", "EQPM", "VESSEL", "TANK", "PUMP", "COMPRESSOR", "EXCHANGER", "FILTER"),
    ),
    # Building columns and steel are obstacles; walls bound the room like a skid frame.
    ("structure", ("S-COLS", "COLUMN", "STRUCT", "STEEL", "S-BEAM", "S-COLM")),
    ("frame", ("FRAME", "BORDER", "WORKSPACE", "SKID", "WALL")),
)
SCHEMATIC_WORDS = re.compile(r"P\s*&\s*ID|P&AMP;ID|PIPING AND INSTRUMENT|PFD|FLOW DIAGRAM")
TEXT_VALUES = {
    "outer_diameter": re.compile(r"\bOD\s*[:=]?\s*(\d+(?:\.\d+)?)"),
    "bend_radius": re.compile(r"BEND\s+RADIUS\s*[:=]?\s*(\d+(?:\.\d+)?)"),
    "clearance": re.compile(r"CLEARANCE\s*[:=]?\s*(\d+(?:\.\d+)?)"),
}


EndKind = Literal["nozzle", "tee", "battery_limit", "open"]


@dataclass(frozen=True)
class LineEnd:
    """How one end of a drawn run is connected.

    ``port`` is where the pipe itself starts (a nozzle's flange face, a tee point on the
    header centreline, or the drawn end) and ``direction`` is the way the pipe leaves it.
    """

    kind: EndKind
    port: Point3
    direction: Point3
    equipment: str | None = None
    face: Point3 | None = None  # where a nozzle stub meets the equipment surface
    note: str = ""


@dataclass(frozen=True)
class DrawnLine:
    """A pipe run as drawn, port to port, as corner points in mm (risers included)."""

    name: str
    points: list[Point3]
    orthogonal: bool
    tube: TubeSpecification | None = None
    line_number: str | None = None
    ends: tuple[LineEnd, ...] = ()
    problems: tuple[str, ...] = ()  # why the run cannot be built as drawn
    rationale: tuple[Rationale, ...] = ()  # why ``tube`` has its values


@dataclass
class InferredLayout:
    supported: bool
    message: str
    workspace: Point3 | None = None
    obstacles: list[BoxObstacle | CylinderObstacle] = field(default_factory=list)
    equipment: list[InferredEquipment] = field(default_factory=list)
    lines: list[DrawnLine] = field(default_factory=list)
    tube: TubeSpecification | None = None
    assumptions: list[Assumption] = field(default_factory=list)
    views: list[str] = field(default_factory=list)
    structures: list[str] = field(default_factory=list)  # obstacle names that are structure
    rationale: list[Rationale] = field(default_factory=list)  # why ``tube`` has its values


@dataclass(frozen=True)
class _Item:
    """One model-space entity with its role and extents, in millimetres."""

    entity: DXFGraphic
    role: LayerRole
    box: Rect

    @property
    def centre(self) -> Point2:
        return ((self.box[0] + self.box[2]) / 2, (self.box[1] + self.box[3]) / 2)


@dataclass(frozen=True)
class _Text:
    text: str
    at: Point2


def layer_role(name: str, overrides: dict[str, LayerRole]) -> LayerRole | None:
    if name in overrides:
        return overrides[name]
    upper = name.upper()
    for role, words in ROLE_WORDS:
        if any(word in upper for word in words):
            return role
    return None


def _inside(point: Point2, rect: Rect, pad: float = 0.0) -> bool:
    return rect[0] - pad <= point[0] <= rect[2] + pad and rect[1] - pad <= point[1] <= rect[3] + pad


def _closed_outline(entity: DXFGraphic) -> list[Point2] | None:
    kind = entity.dxftype()
    if kind == "LWPOLYLINE":
        points = [(float(x), float(y)) for x, y, *_ in entity.get_points()]
        closed = bool(entity.closed) or (len(points) > 3 and points[0] == points[-1])
    elif kind == "POLYLINE" and not entity.is_3d_polyline:
        points = [(float(v.dxf.location.x), float(v.dxf.location.y)) for v in entity.vertices]
        closed = bool(entity.is_closed) or (len(points) > 3 and points[0] == points[-1])
    else:
        return None
    return points if closed and len(points) >= 3 else None


def _is_rectangle(points: list[Point2]) -> bool:
    unique = list(dict.fromkeys(points))
    if len(unique) != 4:
        return False
    xs = sorted({round(x, 6) for x, _ in unique})
    ys = sorted({round(y, 6) for _, y in unique})
    return len(xs) == 2 and len(ys) == 2


def _texts(doc: Drawing, scale: float) -> list[_Text]:
    found: list[_Text] = []
    for entity in doc.modelspace().query("TEXT MTEXT"):
        if entity.dxftype() == "TEXT":
            text = entity.dxf.text
            point = entity.get_placement()[1]
        else:
            text = entity.plain_text()
            point = entity.dxf.insert
        text = " ".join(str(text).split())
        if text:
            found.append(_Text(text, (float(point.x) * scale, float(point.y) * scale)))
    return found


def _items(doc: Drawing, scale: float, overrides: dict[str, LayerRole]) -> list[_Item]:
    items: list[_Item] = []
    for entity in doc.modelspace():
        if entity.dxftype() in {"TEXT", "MTEXT", "DIMENSION", "HATCH"}:
            continue
        role = layer_role(entity.dxf.layer, overrides)
        if role is None or role == "ignore":
            continue
        extents = bbox.extents([entity])
        if not extents.has_data:
            continue
        low, high = extents.extmin, extents.extmax
        box = (low.x * scale, low.y * scale, high.x * scale, high.y * scale)
        items.append(_Item(entity, role, box))
    return items


def _view_frames(items: list[_Item], texts: list[_Text], scale: float) -> dict[str, Rect]:
    """Find labelled views: the closest rectangle directly below or above a view title.

    Syntera's own drawings title a view above it; most drawing standards title it below.
    """
    rectangles: list[Rect] = []
    for item in items:
        outline = _closed_outline(item.entity)
        if item.role == "frame" and outline and _is_rectangle(outline):
            rectangles.append(item.box)
    labels = {
        "plan": re.compile(r"\bPLAN\b"),
        "front": re.compile(r"\bFRONT\b|^SECTION\b|^ELEVATION\b"),
        "side": re.compile(r"\b(RIGHT|LEFT|SIDE|END)\b.*\bELEVATION\b"),
    }
    frames: dict[str, Rect] = {}
    for view, pattern in labels.items():
        # the title nearest its view wins, so a title-block line naming the views loses
        gaps = []
        for text in texts:
            if not pattern.search(text.text.upper()):
                continue
            for rect in rectangles:
                if rect in frames.values() or not rect[0] <= text.at[0] <= rect[2]:
                    continue
                for gap in (text.at[1] - rect[3], rect[1] - text.at[1]):
                    if 0 <= gap <= 0.5 * (rect[3] - rect[1]):
                        gaps.append((gap, rect))
        if gaps:
            frames[view] = min(gaps)[1]
    return frames


def _text_value(texts: list[_Text], key: str) -> float | None:
    for text in sorted(texts, key=lambda t: (t.at[1], t.at[0], t.text)):
        match = TEXT_VALUES[key].search(text.text.upper())
        if match:
            return float(match.group(1))
    return None


def _flatten(entity: DXFGraphic, scale: float, distance: float) -> list[Point2]:
    try:
        path = make_path(entity)
    except TypeError:
        return []
    return [(p.x * scale, p.y * scale) for p in path.flattening(distance / max(scale, 1e-12))]


def _chains(polylines: list[list[Point2]], tolerance: float) -> list[list[Point2]]:
    """Join drawn pieces end to end into continuous runs, starting from free ends."""
    pieces = sorted((piece for piece in polylines if len(piece) >= 2), key=lambda p: (p[0], p[-1]))

    def near(a: Point2, b: Point2) -> bool:
        return math.dist(a, b) <= tolerance

    def degree(point: Point2) -> int:
        return sum(near(point, piece[0]) + near(point, piece[-1]) for piece in pieces)

    used = [False] * len(pieces)
    runs: list[list[Point2]] = []
    order = sorted(
        range(len(pieces)),
        key=lambda i: (min(degree(pieces[i][0]), degree(pieces[i][-1])), pieces[i][0]),
    )
    for first in order:
        if used[first]:
            continue
        used[first] = True
        run = list(pieces[first])
        if degree(run[0]) == 1 and degree(run[-1]) > 1:
            pass
        elif degree(run[-1]) == 1 and degree(run[0]) > 1:
            run.reverse()
        for end in ("tail", "head"):
            extended = True
            while extended:
                extended = False
                tip = run[-1] if end == "tail" else run[0]
                for index, piece in enumerate(pieces):
                    if used[index]:
                        continue
                    if near(tip, piece[0]) or near(tip, piece[-1]):
                        segment = piece if near(tip, piece[0]) else list(reversed(piece))
                        if end == "tail":
                            run.extend(segment[1:])
                        else:
                            run[:0] = list(reversed(segment))[:-1]
                        used[index] = True
                        extended = True
                        break
        runs.append(run)
    return runs


def _unit(a: Point2, b: Point2) -> Point2:
    length = math.dist(a, b)
    return ((b[0] - a[0]) / length, (b[1] - a[1]) / length)


def _intersection(a0: Point2, a1: Point2, b0: Point2, b1: Point2) -> Point2 | None:
    d1 = (a1[0] - a0[0], a1[1] - a0[1])
    d2 = (b1[0] - b0[0], b1[1] - b0[1])
    denominator = d1[0] * d2[1] - d1[1] * d2[0]
    if abs(denominator) <= 1e-9 * math.hypot(*d1) * math.hypot(*d2):
        return None
    t = ((b0[0] - a0[0]) * d2[1] - (b0[1] - a0[1]) * d2[0]) / denominator
    return (a0[0] + t * d1[0], a0[1] + t * d1[1])


def corner_points(run: list[Point2], minimum_straight: float, tolerance: float) -> list[Point2]:
    """Reduce a drawn centreline (straights plus sampled bends) to its corner points.

    Straights shorter than ``minimum_straight`` are treated as bend samples; each corner
    is where the straights either side of a bend would meet.
    """
    points: list[Point2] = []
    for point in run:
        if not points or math.dist(point, points[-1]) > tolerance * 0.1:
            points.append(point)
    if len(points) < 2:
        return points
    straights: list[list[Point2]] = []
    for a, b in zip(points, points[1:], strict=False):
        if straights:
            previous = straights[-1]
            u, v = _unit(previous[0], previous[1]), _unit(a, b)
            if u[0] * v[0] + u[1] * v[1] > math.cos(math.radians(1.0)):
                previous[1] = b
                continue
        straights.append([a, b])
    kept = [s for s in straights if math.dist(*s) >= minimum_straight] or straights
    corners = [points[0]]
    for first, second in zip(kept, kept[1:], strict=False):
        meet = _intersection(first[0], first[1], second[0], second[1])
        if meet is None:
            corners.extend([first[1], second[0]])
        else:
            corners.append(meet)
    corners.append(points[-1])
    return _snap_orthogonal(_drop_collinear(corners, tolerance), tolerance)


def _drop_collinear(points: list[Point2], tolerance: float) -> list[Point2]:
    kept: list[Point2] = []
    for point in points:
        if kept and math.dist(point, kept[-1]) <= tolerance:
            continue
        if len(kept) >= 2:
            u, v = _unit(kept[-2], kept[-1]), _unit(kept[-1], point)
            if abs(u[0] * v[1] - u[1] * v[0]) < math.sin(math.radians(1.0)) and (
                u[0] * v[0] + u[1] * v[1] > 0
            ):
                kept[-1] = point
                continue
        kept.append(point)
    return kept


def _snap_orthogonal(points: list[Point2], tolerance: float) -> list[Point2]:
    """Square up legs that are within 1 degree of an axis (drawing round-off)."""
    snapped = [points[0]]
    for point in points[1:]:
        last = snapped[-1]
        dx, dy = point[0] - last[0], point[1] - last[1]
        length = math.hypot(dx, dy)
        if abs(dy) <= max(tolerance * 0.5, length * math.sin(math.radians(1.0))):
            point = (point[0], last[1])
        elif abs(dx) <= max(tolerance * 0.5, length * math.sin(math.radians(1.0))):
            point = (last[0], point[1])
        snapped.append(point)
    return snapped


def is_orthogonal(points: list[Point3]) -> bool:
    for a, b in zip(points, points[1:], strict=False):
        if sum(abs(b[i] - a[i]) > 1e-6 for i in range(3)) != 1:
            return False
    for a, b, c in zip(points, points[1:], points[2:], strict=False):
        first = [b[i] - a[i] for i in range(3)]
        second = [c[i] - b[i] for i in range(3)]
        if abs(sum(first[i] * second[i] for i in range(3))) > 1e-6:
            return False
    return True


def _label_inside(texts: Iterable[_Text], rect: Rect, used: set[str]) -> str | None:
    """An equipment tag inside the outline if there is one, else the lowest short text."""
    inside = [
        t
        for t in sorted(texts, key=lambda t: (t.at[1], t.at[0], t.text))
        if _inside(t.at, rect) and t.text not in used and len(t.text) <= 60
    ]
    tags = [t for t in inside if EQUIPMENT_TAG.match(t.text.upper())]
    chosen = tags or inside
    return chosen[0].text if chosen else None


def _source(user_value: float | None, drawing_value: float | None) -> str:
    if user_value is not None:
        return "user"
    return "drawing" if drawing_value is not None else "default"


def tube_rationale(
    tube: TubeSpecification,
    *,
    od_source: str,
    radius_source: str,
    radius_clamped: bool,
    clearance_source: str,
) -> list[Rationale]:
    """Why the inferred tube has its OD, bend radius and clearance (user, drawing or rule)."""
    stated = {
        "user": ("user", None, "as given in the check options."),
        "drawing": ("drawing", None, "as drawn or annotated on the drawing."),
    }
    od = tube.outer_diameter
    records = []
    for path, value, source, default_rule, default_text in (
        (
            "tube.outer_diameter",
            od,
            od_source,
            None,
            "not given or drawn; DN50 (60.3 mm OD) is assumed.",
        ),
        (
            "tube.minimum_bend_radius",
            tube.minimum_bend_radius,
            radius_source,
            "drawing.bend_radius_long_radius.v1",
            "not given or drawn; 1.5 x the {outer_diameter_mm:g} mm OD (long-radius elbow).",
        ),
        (
            "tube.minimum_clearance",
            tube.minimum_clearance,
            clearance_source,
            "drawing.clearance_half_od.v1",
            "not given or drawn; half the {outer_diameter_mm:g} mm OD.",
        ),
    ):
        kind, rule, text = stated.get(
            source, ("rule" if default_rule else "default", default_rule, default_text)
        )
        if path == "tube.minimum_bend_radius" and radius_clamped:
            kind, rule = "rule", "drawing.bend_radius_at_least_tube_radius.v1"
            text = (
                "raised to the tube radius, the smallest bend a {outer_diameter_mm:g} mm OD allows."
            )
        records.append(
            Rationale(
                path=path,
                value=value,
                unit="mm",
                source=kind,  # type: ignore[arg-type]
                rule_id=rule,
                inputs={"outer_diameter_mm": od},
                explanation="{value} {unit}: " + text,
            )
        )
    return records


def infer_layout(
    doc: Drawing, mm_per_unit: float | None, options: AssemblyOptions
) -> InferredLayout:
    """Read equipment, pipe runs and (where drawn) elevations into a 3D layout in mm."""
    assumptions: list[Assumption] = []
    scale = mm_per_unit or 1.0
    if not mm_per_unit:
        assumptions.append(
            Assumption(
                quantity="Drawing units",
                value="millimetres",
                source="default",
                note="The drawing does not declare units, so one drawing unit is read as 1 mm.",
            )
        )
    texts = _texts(doc, scale)
    items = _items(doc, scale, options.layer_roles)
    roles = {item.role for item in items}
    has_pipes = bool(roles & {"pipe", "pipe_centreline"})
    if "equipment" not in roles or not has_pipes:
        schematic = any(SCHEMATIC_WORDS.search(t.text.upper()) for t in texts)
        missing = [
            label
            for label, present in (("equipment", "equipment" in roles), ("pipe", has_pipes))
            if not present
        ]
        if schematic:
            reason = (
                "This looks like a P&ID or other schematic. It shows how lines connect, not "
                "where equipment and pipes sit, so it cannot define a 3D assembly."
            )
        elif not roles:
            reason = "Syntera found no layers named like equipment or piping on this drawing."
        else:
            reason = (
                "Syntera could not find "
                + " or ".join(f"{label} geometry" for label in missing)
                + " on this drawing."
            )
        return InferredLayout(
            supported=False,
            message=reason
            + " A plan layout works: equipment outlines on a layer named like EQUIPMENT, "
            "VESSEL or PUMP, and pipe runs on a layer named like PIPE. Layers with other "
            "names can be given a role under Layer roles. For a P&ID, upload its DEXPI XML "
            "export instead to get a proposed 3D layout.",
            assumptions=assumptions,
        )

    frames = _view_frames(items, texts, scale)
    views = [name for name in ("plan", "front", "side") if name in frames]
    plan = frames.get("plan")
    front = frames.get("front")
    if (
        front is not None
        and plan is not None
        and abs((front[2] - front[0]) - (plan[2] - plan[0])) > 1e-3 * max(1.0, plan[2] - plan[0])
    ):
        assumptions.append(
            Assumption(
                quantity="Front elevation",
                value="not used",
                source="default",
                note="Its width does not match the plan, so heights could not be read from it.",
            )
        )
        front = None
    plan_items = [i for i in items if plan is None or _inside(i.centre, plan)]
    plan_texts = [t for t in texts if plan is None or _inside(t.at, plan)]
    front_items = [i for i in items if front is not None and _inside(i.centre, front)]

    # ---- tube specification: user value, else drawing, else default ----
    widths = [
        float(i.entity.dxf.const_width) * scale
        for i in plan_items
        if i.role == "pipe"
        and i.entity.dxftype() == "LWPOLYLINE"
        and i.entity.dxf.hasattr("const_width")
    ]
    nozzle_circles = [
        i for i in plan_items if i.role == "nozzle" and i.entity.dxftype() == "CIRCLE"
    ]
    drawn_od = max(widths) if widths and max(widths) > 0 else _text_value(texts, "outer_diameter")
    if drawn_od is None and nozzle_circles:
        drawn_od = min(2 * float(i.entity.dxf.radius) * scale for i in nozzle_circles)
    od = options.outer_diameter_mm or drawn_od or DEFAULT_OUTER_DIAMETER_MM
    arc_radii = sorted(
        float(i.entity.dxf.radius) * scale
        for i in plan_items
        if i.role in {"pipe", "pipe_centreline"} and i.entity.dxftype() == "ARC"
    )
    drawn_radius = _text_value(texts, "bend_radius") or (arc_radii[0] if arc_radii else None)
    chosen_radius = options.bend_radius_mm or drawn_radius or 1.5 * od
    radius = max(chosen_radius, od / 2)
    drawn_clearance = _text_value(texts, "clearance")
    clearance = (
        options.clearance_mm
        if options.clearance_mm is not None
        else drawn_clearance
        if drawn_clearance is not None
        else od / 2
    )
    tube = TubeSpecification(
        outer_diameter=od, minimum_bend_radius=radius, minimum_clearance=clearance
    )
    assumptions += [
        Assumption(
            quantity="Pipe outside diameter",
            value=f"{od:g} mm",
            source=_source(options.outer_diameter_mm, drawn_od),
            note="" if options.outer_diameter_mm or drawn_od else "DN50 assumed.",
        ),
        Assumption(
            quantity="Bend radius",
            value=f"{radius:g} mm",
            source=_source(options.bend_radius_mm, drawn_radius),
            note="" if options.bend_radius_mm or drawn_radius else "1.5 x OD (long-radius elbow).",
        ),
        Assumption(
            quantity="Required clearance",
            value=f"{clearance:g} mm",
            source=_source(options.clearance_mm, drawn_clearance),
            note=""
            if options.clearance_mm is not None or drawn_clearance is not None
            else "Half the pipe OD.",
        ),
    ]

    # ---- plan origin: the plan frame when drawn, else the drawing's own extents ----
    footprint_items = [i for i in plan_items if i.role in {"equipment", "pipe", "pipe_centreline"}]
    if plan is not None:
        origin = (plan[0], plan[1])
        extent = (plan[2] - plan[0], plan[3] - plan[1])
        margin = 0.0
    else:
        low = (min(i.box[0] for i in footprint_items), min(i.box[1] for i in footprint_items))
        high = (max(i.box[2] for i in footprint_items), max(i.box[3] for i in footprint_items))
        margin = max(2 * (od / 2 + clearance), 0.05 * max(high[0] - low[0], high[1] - low[1]))
        # Whole-100 mm origin so nozzles drawn on round coordinates stay on the routing grid.
        origin = tuple(
            math.floor((value - margin) / ORIGIN_STEP_MM) * ORIGIN_STEP_MM for value in low
        )
        extent = tuple(
            math.ceil((high[i] + margin - origin[i]) / ORIGIN_STEP_MM) * ORIGIN_STEP_MM
            for i in range(2)
        )
        assumptions.append(
            Assumption(
                quantity="Skid limits",
                value=f"{extent[0]:.0f} x {extent[1]:.0f} mm",
                source="default",
                note=f"No plan frame found; the drawn content plus at least {margin:.0f} mm.",
            )
        )

    def local(point: Point2) -> Point2:
        return (point[0] - origin[0], point[1] - origin[1])

    def front_z_range(
        x0: float, x1: float, role: LayerRole = "equipment"
    ) -> tuple[float, float] | None:
        """Vertical extent of the front-elevation items of ``role`` spanning the same X range."""
        if front is None:
            return None
        tolerance = max(1.0, 0.01 * (x1 - x0))
        spans = [
            (i.box[1] - front[1], i.box[3] - front[1])
            for i in front_items
            if i.role == role
            and abs(i.box[0] - front[0] - x0) <= tolerance
            and abs(i.box[2] - front[0] - x1) <= tolerance
        ]
        if not spans:
            return None
        return (min(s[0] for s in spans), max(s[1] for s in spans))

    # ---- equipment footprints ----
    default_height = options.equipment_height_mm or DEFAULT_EQUIPMENT_HEIGHT_MM
    obstacles: list[BoxObstacle | CylinderObstacle] = []
    equipment: list[InferredEquipment] = []
    used_names: set[str] = set()
    plan_equipment = sorted(
        (i for i in plan_items if i.role == "equipment"), key=lambda i: (i.box, i.entity.dxf.handle)
    )
    for item in plan_equipment:
        kind = item.entity.dxftype()
        x0, y0 = local((item.box[0], item.box[1]))
        x1, y1 = local((item.box[2], item.box[3]))
        if x1 - x0 <= 0 or y1 - y0 <= 0:
            continue
        span = None if options.equipment_height_mm else front_z_range(x0, x1)
        z0, z1 = span if span and span[1] > span[0] else (0.0, default_height)
        height_source = "user" if options.equipment_height_mm else "drawing" if span else "default"
        name = _label_inside(plan_texts, item.box, used_names)
        if name is None and kind == "INSERT":
            name = item.entity.dxf.name
        name = name or f"equipment-{len(obstacles) + 1}"
        used_names.add(name)
        if kind == "CIRCLE":
            obstacles.append(
                CylinderObstacle(
                    type="cylinder",
                    name=name,
                    center=((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2),
                    radius=(x1 - x0) / 2,
                    height=z1 - z0,
                    axis="z",
                )
            )
            shape = "cylinder"
        else:
            obstacles.append(
                BoxObstacle(
                    type="box",
                    name=name,
                    center=((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2),
                    size=(x1 - x0, y1 - y0, z1 - z0),
                )
            )
            shape = "box"
        equipment.append(
            InferredEquipment(
                name=name,
                shape=shape,
                layer=item.entity.dxf.layer,
                plan_handle=item.entity.dxf.handle,
                height_source=height_source,
            )
        )
    defaulted = [e.name for e in equipment if e.height_source == "default"]
    if defaulted:
        assumptions.append(
            Assumption(
                quantity="Equipment height",
                value=f"{default_height:g} mm from the skid floor",
                source="default",
                note=f"No elevation gives the height of: {', '.join(defaulted)}.",
            )
        )
    elif options.equipment_height_mm:
        assumptions.append(
            Assumption(
                quantity="Equipment height",
                value=f"{default_height:g} mm from the skid floor",
                source="user",
            )
        )

    # ---- pipe runs ----
    centreline = [i for i in plan_items if i.role == "pipe_centreline"]
    pipe_items = centreline or [i for i in plan_items if i.role == "pipe"]
    tolerance = max(0.5, od * 0.05)
    pieces = []
    for item in sorted(pipe_items, key=lambda i: (i.box, i.entity.dxf.handle)):
        piece = [local(p) for p in _flatten(item.entity, scale, max(0.05, radius * 0.02))]
        # a closed curve on a pipe layer is a symbol (riser, cap, reducer), not a run
        if len(piece) > 2 and math.dist(piece[0], piece[-1]) <= tolerance:
            continue
        pieces.append(piece)
    runs = [
        run
        for run in _chains(pieces, tolerance)
        if math.dist(run[0], run[-1]) > tolerance or len(run) > 2
    ]
    runs.sort(
        key=lambda run: (-sum(math.dist(a, b) for a, b in zip(run, run[1:], strict=False)), run[0])
    )
    if len(runs) > MAX_LINES:
        assumptions.append(
            Assumption(
                quantity="Pipe lines checked",
                value=f"{MAX_LINES} longest of {len(runs)}",
                source="default",
            )
        )
        runs = runs[:MAX_LINES]
    plans = []
    for run in runs:
        corners = corner_points(run, radius * 0.3, tolerance)
        if len(corners) < 2:
            continue
        if corners[-1] < corners[0]:
            corners.reverse()
        plans.append(corners)

    # ---- line numbers and elevations written beside the runs ----
    equipment_boxes = [i.box for i in plan_items if i.role == "equipment"]
    numbers: dict[int, list[tuple[float, LineNumber]]] = {}
    levels: dict[tuple[int, int], list[tuple[float, Elevation]]] = {}
    for text in plan_texts:
        number = parse_line_number(text.text)
        level = parse_elevation(text.text)
        if level is not None and any(_inside(text.at, box) for box in equipment_boxes):
            level = None  # TOP EL inside an outline describes the equipment, not a pipe
        if number is None and level is None:
            continue
        nearest = _nearest_segment(plans, local(text.at))
        if nearest is None or nearest[0] > LABEL_REACH_MM:
            continue
        distance, run_index, segment = nearest
        if number is not None:
            numbers.setdefault(run_index, []).append((distance, number))
        if level is not None:
            levels.setdefault((run_index, segment), []).append((distance, level))

    fallback = options.pipe_elevation_mm
    fallback_source = "user"
    if fallback is None and front is not None:
        front_nozzles = sorted(
            (i for i in front_items if i.role == "nozzle" and i.entity.dxftype() == "CIRCLE"),
            key=lambda i: i.centre,
        )
        for corners in plans:
            for nozzle in front_nozzles:
                if abs(nozzle.centre[0] - front[0] - corners[0][0]) <= tolerance:
                    fallback, fallback_source = nozzle.centre[1] - front[1], "drawing"
                    break
            if fallback is not None:
                break
    if fallback is None:
        fallback, fallback_source = DEFAULT_PIPE_ELEVATION_MM, "default"

    specs: list[tuple[str | None, TubeSpecification]] = []
    why: list[tuple[Rationale, ...]] = []
    paths: list[list[Point3]] = []
    labelled_levels = 0
    for index, corners in enumerate(plans):
        number = min(numbers[index], key=lambda pair: pair[0])[1] if index in numbers else None
        line_od = (
            options.outer_diameter_mm
            or (number.outer_diameter_mm if number else None)
            or drawn_od
            or DEFAULT_OUTER_DIAMETER_MM
        )
        chosen_line_radius = options.bend_radius_mm or drawn_radius or 1.5 * line_od
        line_radius = max(chosen_line_radius, line_od / 2)
        line_clearance = (
            options.clearance_mm
            if options.clearance_mm is not None
            else drawn_clearance
            if drawn_clearance is not None
            else line_od / 2
        )
        spec = TubeSpecification(
            outer_diameter=line_od,
            minimum_bend_radius=line_radius,
            minimum_clearance=line_clearance,
        )
        specs.append((number.text if number else None, spec))
        why.append(
            tuple(
                tube_rationale(
                    spec,
                    od_source=_source(
                        options.outer_diameter_mm,
                        (number.outer_diameter_mm if number else None) or drawn_od,
                    ),
                    radius_source=_source(options.bend_radius_mm, drawn_radius),
                    radius_clamped=line_radius != chosen_line_radius,
                    clearance_source=_source(options.clearance_mm, drawn_clearance),
                )
            )
        )
        heights: list[float | None] = []
        for segment in range(len(corners) - 1):
            found = levels.get((index, segment))
            if options.pipe_elevation_mm is None and found:
                heights.append(min(found, key=lambda pair: pair[0])[1].centreline(line_od))
                labelled_levels += 1
            else:
                heights.append(None)
        paths.append(_rise(corners, _fill(heights, fallback)))

    lines: list[DrawnLine] = []
    battery = (extent[0], extent[1])
    connectors = [
        (i.box[0] - origin[0], i.box[1] - origin[1], i.box[2] - origin[0], i.box[3] - origin[1])
        for i in plan_items
        if i.role == "nozzle"
    ]
    for index, path in enumerate(paths):
        number, spec = specs[index]
        ends: list[LineEnd] = []
        problems: list[str] = []
        for reverse in (False, True):
            path = list(reversed(path)) if reverse else path
            path, end, problem = _attach(
                path, index, paths, obstacles, battery, connectors, spec, tolerance
            )
            path = list(reversed(path)) if reverse else path
            ends.append(end)
            problems += [problem] if problem else []
        points3 = [(round(x, 6), round(y, 6), round(z, 6)) for x, y, z in path]
        lines.append(
            DrawnLine(
                name=number or f"Line {index + 1}",
                points=points3,
                orthogonal=is_orthogonal(points3),
                tube=spec,
                line_number=number,
                ends=tuple(ends),
                problems=tuple(problems),
                rationale=why[index],
            )
        )
    named = [line.line_number for line in lines if line.line_number]
    if named:
        assumptions[:] = [
            a.model_copy(update={"note": (a.note + " Used for runs with no line number.").strip()})
            if a.quantity in {"Pipe outside diameter", "Bend radius", "Required clearance"}
            else a
            for a in assumptions
        ]
        sizes = sorted({spec.outer_diameter for _, spec in specs})
        assumptions.append(
            Assumption(
                quantity="Line sizes",
                value=", ".join(f"{size:g}" for size in sizes) + " mm OD",
                source="user" if options.outer_diameter_mm else "drawing",
                note=f"Read from {len(named)} of {len(lines)} line numbers (ASME B36.10 OD).",
            )
        )
    level_source = (
        "user" if options.pipe_elevation_mm else "drawing" if labelled_levels else (fallback_source)
    )
    assumptions.append(
        Assumption(
            quantity="Pipe centreline elevation",
            value=f"{fallback:g} mm"
            if not labelled_levels
            else f"{labelled_levels} elevation label(s) read",
            source=level_source,  # type: ignore[arg-type]
            note="Runs change level at the corner between legs with different elevation "
            "labels; a leg with no label takes its neighbour's level"
            + ("" if labelled_levels else f", and a run with none sits at {fallback:g} mm")
            + ".",
        )
    )
    nozzles = sum(end.kind == "nozzle" for line in lines for end in line.ends)
    if nozzles:
        assumptions.append(
            Assumption(
                quantity="Nozzle projection",
                value=f"{NOZZLE_PROJECTION_MM:g} mm",
                source="default",
                note=f"{nozzles} run ends meet equipment; a run ending inside an outline above "
                "or below the equipment drops or rises to its top or bottom nozzle.",
            )
        )
    valves = [
        i
        for i in plan_items
        if "VALV" in i.entity.dxf.layer.upper()
        or (i.entity.dxftype() == "INSERT" and "VALVE" in i.entity.dxf.name.upper())
    ]
    if not valves:
        valves = [
            entity
            for entity in doc.modelspace().query("INSERT")
            if "VALV" in entity.dxf.layer.upper() or "VALVE" in entity.dxf.name.upper()
        ]
    if valves:
        assumptions.append(
            Assumption(
                quantity="Valves",
                value=f"{len(valves)} drawn, modelled as straight pipe",
                source="default",
                note="Valve bodies, handwheels and their access are not checked yet.",
            )
        )
    if not lines:
        return InferredLayout(
            supported=False,
            message="Syntera found pipe layers but no continuous pipe run in the plan.",
            assumptions=assumptions,
            views=views,
        )

    tops = [
        o.center[2] + (o.size[2] / 2 if isinstance(o, BoxObstacle) else o.height / 2)
        for o in obstacles
    ]
    if front is not None and options.equipment_height_mm is None:
        height = front[3] - front[1]
    else:
        highest = max(z + line.tube.outer_diameter / 2 + line.tube.minimum_clearance
                      for line in lines for *_, z in line.points)  # fmt: skip
        height = max([*tops, highest]) + max(2 * (od / 2 + clearance), 0.1 * max([*tops, highest]))

    # ---- building columns and steel: full height unless an elevation shows them ----
    structures: list[str] = []
    for item in sorted(
        (i for i in plan_items if i.role == "structure" and _closed_outline(i.entity)),
        key=lambda i: (i.box, i.entity.dxf.handle),
    ):
        x0, y0 = local((item.box[0], item.box[1]))
        x1, y1 = local((item.box[2], item.box[3]))
        if x1 - x0 <= 0 or y1 - y0 <= 0:
            continue
        span = front_z_range(x0, x1, "structure")
        z0, z1 = span if span and span[1] > span[0] else (0.0, height)
        name = f"{item.entity.dxf.layer} at {(x0 + x1) / 2:.0f}, {(y0 + y1) / 2:.0f}"
        obstacles.append(
            BoxObstacle(
                type="box",
                name=name,
                center=((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2),
                size=(x1 - x0, y1 - y0, z1 - z0),
            )
        )
        structures.append(name)
    if structures:
        assumptions.append(
            Assumption(
                quantity="Structure",
                value=f"{len(structures)} column(s) or steel member(s)",
                source="drawing",
                note="Height from the elevation where drawn there, else floor to roof.",
            )
        )
    return InferredLayout(
        supported=True,
        message=f"Read {len(equipment)} equipment item(s), {len(structures)} structural "
        f"member(s) and {len(lines)} pipe run(s).",
        workspace=(round(extent[0], 6), round(extent[1], 6), round(height, 6)),
        obstacles=obstacles,
        equipment=equipment,
        lines=lines,
        tube=tube,
        assumptions=assumptions,
        views=views,
        structures=structures,
        rationale=tube_rationale(
            tube,
            od_source=_source(options.outer_diameter_mm, drawn_od),
            radius_source=_source(options.bend_radius_mm, drawn_radius),
            radius_clamped=radius != chosen_radius,
            clearance_source=_source(options.clearance_mm, drawn_clearance),
        ),
    )


def _segment_distance(point: Point2, a: Point2, b: Point2) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = dx * dx + dy * dy
    t = 0.0 if length == 0 else ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / length
    t = min(1.0, max(0.0, t))
    return math.dist(point, (a[0] + t * dx, a[1] + t * dy))


def _nearest_segment(plans: list[list[Point2]], point: Point2) -> tuple[float, int, int] | None:
    """(distance, run, segment) of the plan segment closest to ``point``; ties go to the first."""
    best = None
    for run, corners in enumerate(plans):
        for segment, (a, b) in enumerate(zip(corners, corners[1:], strict=False)):
            candidate = (round(_segment_distance(point, a, b), 6), run, segment)
            if best is None or candidate < best:
                best = candidate
    return best


def _fill(heights: list[float | None], fallback: float) -> list[float]:
    """Unlabelled legs take the level of the nearest labelled leg before them, else after."""
    filled = list(heights)
    last = None
    for index, value in enumerate(filled):
        if value is None:
            filled[index] = last
        else:
            last = value
    nxt = None
    for index in range(len(filled) - 1, -1, -1):
        if heights[index] is not None:
            nxt = heights[index]
        if filled[index] is None:
            filled[index] = nxt
    return [fallback if value is None else value for value in filled]


def _rise(corners: list[Point2], heights: list[float]) -> list[Point3]:
    """3D corner points: each leg at its own level, with a vertical riser where levels change."""
    points: list[Point3] = [(corners[0][0], corners[0][1], heights[0])]
    for index in range(1, len(corners) - 1):
        x, y = corners[index]
        points.append((x, y, heights[index - 1]))
        if abs(heights[index] - heights[index - 1]) > 1e-6:
            points.append((x, y, heights[index]))
    points.append((corners[-1][0], corners[-1][1], heights[-1]))
    return points


def _along(path: list[Point3], point: Point3) -> list[Point3]:
    """Prepend ``point`` to the path, merging it into the first leg when collinear."""
    if math.dist(point, path[0]) <= 1e-6:
        return path
    if len(path) >= 2:
        first = [path[1][i] - path[0][i] for i in range(3)]
        new = [path[0][i] - point[i] for i in range(3)]
        cross = (
            first[1] * new[2] - first[2] * new[1],
            first[2] * new[0] - first[0] * new[2],
            first[0] * new[1] - first[1] * new[0],
        )
        if math.hypot(*cross) <= 1e-6 * max(1.0, math.hypot(*first) * math.hypot(*new)) and (
            sum(first[i] * new[i] for i in range(3)) > 0
        ):
            return [point, *path[1:]]
    return [point, *path]


def _unit3(a: Point3, b: Point3) -> Point3:
    length = math.dist(a, b)
    return tuple(round((b[i] - a[i]) / length, 9) for i in range(3))  # type: ignore[return-value]


def _span(obstacle: BoxObstacle | CylinderObstacle) -> tuple[float, float]:
    half = obstacle.size[2] / 2 if isinstance(obstacle, BoxObstacle) else obstacle.height / 2
    return (obstacle.center[2] - half, obstacle.center[2] + half)


def _plan_depth(obstacle: BoxObstacle | CylinderObstacle, point: Point2) -> tuple[float, Point2]:
    """Signed plan distance inside the outline (positive inside) and the outward normal there."""
    cx, cy = obstacle.center[0], obstacle.center[1]
    if isinstance(obstacle, CylinderObstacle):
        offset = math.dist(point, (cx, cy))
        normal = ((point[0] - cx) / offset, (point[1] - cy) / offset) if offset else (1.0, 0.0)
        return obstacle.radius - offset, normal
    half = (obstacle.size[0] / 2, obstacle.size[1] / 2)
    depths = [
        (half[0] - (point[0] - cx), (1.0, 0.0)),
        (half[0] + (point[0] - cx), (-1.0, 0.0)),
        (half[1] - (point[1] - cy), (0.0, 1.0)),
        (half[1] + (point[1] - cy), (0.0, -1.0)),
    ]
    return min(depths)


def _attach(
    path: list[Point3],
    index: int,
    paths: list[list[Point3]],
    obstacles: list[BoxObstacle | CylinderObstacle],
    limits: Point2,
    connectors: list[Rect],
    spec: TubeSpecification,
    tolerance: float,
) -> tuple[list[Point3], LineEnd, str | None]:
    """Work out what the start of ``path`` connects to and complete the path to it."""
    x, y, z = path[0]
    # a tee: the end lies on another run (the header), possibly at another level
    for other, header in enumerate(paths):
        if other == index:
            continue
        for a, b in zip(header, header[1:], strict=False):
            if _segment_distance((x, y), a[:2], b[:2]) > tolerance:
                continue
            if abs(a[2] - b[2]) > 1e-6:  # a riser: join at the nearest level on it
                level = min(max(z, min(a[2], b[2])), max(a[2], b[2]))
            elif min(math.dist((x, y), a[:2]), math.dist((x, y), b[:2])) <= tolerance and (
                header[0] in (a, b) or header[-1] in (a, b)
            ):
                continue  # end to end: not a tee
            else:
                level = a[2]
            path = _along(path, (x, y, level))
            return path, LineEnd("tee", path[0], _unit3(path[0], path[1])), None
    # equipment: on the outline is a side nozzle; inside it, a top or bottom nozzle
    for obstacle in sorted(obstacles, key=lambda o: o.name):
        depth, normal = _plan_depth(obstacle, (x, y))
        if depth < -tolerance:
            continue
        low, high = _span(obstacle)
        if depth <= tolerance:
            if not low <= z <= high:
                note = (
                    f"ends on the outline of {obstacle.name} at EL {z:.0f} mm, but "
                    f"{obstacle.name} spans EL {low:.0f} to {high:.0f} mm"
                )
                return path, LineEnd("open", path[0], _unit3(path[0], path[1]), note=note), note
            face = (x, y, z)
            outward: Point3 = (normal[0], normal[1], 0.0)
        elif z > high:
            face, outward = (x, y, high), (0.0, 0.0, 1.0)
        elif z < low:
            face, outward = (x, y, low), (0.0, 0.0, -1.0)
        else:
            note = f"ends inside {obstacle.name} at EL {z:.0f} mm"
            return path, LineEnd("open", path[0], _unit3(path[0], path[1]), note=note), note
        path = _along(path, face)
        leg = math.dist(path[0], path[1])
        along = _unit3(path[0], path[1])
        if sum(along[i] * outward[i] for i in range(3)) < 1 - 1e-6:
            note = f"leaves {obstacle.name} at an angle to its surface, not along a nozzle"
            end = LineEnd("nozzle", face, along, obstacle.name, face, note)
            return path, end, note
        if leg < NOZZLE_PROJECTION_MM - 1e-6:
            note = (
                f"its leg from {obstacle.name} is {leg:.0f} mm, shorter than the "
                f"{NOZZLE_PROJECTION_MM:g} mm nozzle"
            )
            end = LineEnd("nozzle", face, along, obstacle.name, face, note)
            return path, end, note
        port = tuple(face[i] + outward[i] * NOZZLE_PROJECTION_MM for i in range(3))
        path = [port, *path[1:]] if leg > NOZZLE_PROJECTION_MM + 1e-6 else path[1:]
        if len(path) < 2:
            note = f"is only the {NOZZLE_PROJECTION_MM:g} mm nozzle on {obstacle.name}"
            return [face, port], LineEnd("nozzle", port, outward, obstacle.name, face), note
        end = LineEnd("nozzle", path[0], outward, obstacle.name, face)  # type: ignore[arg-type]
        return path, end, None
    # a battery limit at the room or skid edge, or a drawn connection (drain hub, BL flange)
    reach = BATTERY_LIMIT_REACH_MM
    at_edge = min(x, y, limits[0] - x, limits[1] - y) <= reach
    if at_edge or any(_inside((x, y), box, tolerance) for box in connectors):
        what = "the room or skid limit" if at_edge else "a drawn connection"
        end = LineEnd("battery_limit", path[0], _unit3(path[0], path[1]), note=f"at {what}")
        return path, end, None
    note = "ends in open space, connected to nothing on the drawing"
    return path, LineEnd("open", path[0], _unit3(path[0], path[1]), note=note), None
