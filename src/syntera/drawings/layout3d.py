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

from ezdxf import bbox
from ezdxf.document import Drawing
from ezdxf.entities import DXFGraphic
from ezdxf.path import make_path

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
MAX_LINES = 6
ORIGIN_STEP_MM = 100.0

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
    ("frame", ("FRAME", "BORDER", "WORKSPACE", "SKID")),
)
SCHEMATIC_WORDS = re.compile(r"P\s*&\s*ID|P&AMP;ID|PIPING AND INSTRUMENT|PFD|FLOW DIAGRAM")
TEXT_VALUES = {
    "outer_diameter": re.compile(r"\bOD\s*[:=]?\s*(\d+(?:\.\d+)?)"),
    "bend_radius": re.compile(r"BEND\s+RADIUS\s*[:=]?\s*(\d+(?:\.\d+)?)"),
    "clearance": re.compile(r"CLEARANCE\s*[:=]?\s*(\d+(?:\.\d+)?)"),
}


@dataclass(frozen=True)
class DrawnLine:
    """A pipe run as drawn: its plan path at one elevation, as corner points in mm."""

    name: str
    points: list[Point3]
    orthogonal: bool


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
    rationale: list[Rationale] = field(default_factory=list)


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
    """Find labelled views: the closest rectangle directly below a PLAN/ELEVATION title."""
    rectangles: list[Rect] = []
    for item in items:
        outline = _closed_outline(item.entity)
        if item.role == "frame" and outline and _is_rectangle(outline):
            rectangles.append(item.box)
    labels = {
        "plan": re.compile(r"\bPLAN\b"),
        "front": re.compile(r"\bFRONT\b"),
        "side": re.compile(r"\b(RIGHT|LEFT|SIDE|END)\b.*\bELEVATION\b"),
    }
    frames: dict[str, Rect] = {}
    for view, pattern in labels.items():
        for text in sorted(texts, key=lambda t: (t.at[1], t.at[0])):
            if not pattern.search(text.text.upper()):
                continue
            candidates = [
                rect
                for rect in rectangles
                if rect[0] <= text.at[0] <= rect[2]
                and 0 <= text.at[1] - rect[3] <= 0.5 * (rect[3] - rect[1])
            ]
            if candidates:
                frames[view] = min(candidates, key=lambda rect: (text.at[1] - rect[3], rect))
                break
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
    for text in sorted(texts, key=lambda t: (t.at[1], t.at[0], t.text)):
        if _inside(text.at, rect) and text.text not in used and len(text.text) <= 60:
            return text.text
    return None


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

    def front_z_range(x0: float, x1: float) -> tuple[float, float] | None:
        """Vertical extent of the front-elevation equipment spanning the same X range."""
        if front is None:
            return None
        tolerance = max(1.0, 0.01 * (x1 - x0))
        spans = [
            (i.box[1] - front[1], i.box[3] - front[1])
            for i in front_items
            if i.role == "equipment"
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
    pieces = [
        [local(p) for p in _flatten(i.entity, scale, max(0.05, radius * 0.02))]
        for i in sorted(pipe_items, key=lambda i: (i.box, i.entity.dxf.handle))
    ]
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

    elevation, elevation_source = options.pipe_elevation_mm, "user"
    lines: list[DrawnLine] = []
    for index, run in enumerate(runs):
        corners = corner_points(run, radius * 0.3, tolerance)
        if len(corners) < 2:
            continue
        if corners[-1] < corners[0]:
            corners.reverse()
        if elevation is None and front is not None:
            front_nozzles = sorted(
                (i for i in front_items if i.role == "nozzle" and i.entity.dxftype() == "CIRCLE"),
                key=lambda i: i.centre,
            )
            for nozzle in front_nozzles:
                if abs(nozzle.centre[0] - front[0] - corners[0][0]) <= tolerance:
                    elevation, elevation_source = nozzle.centre[1] - front[1], "drawing"
                    break
        z = elevation if elevation is not None else DEFAULT_PIPE_ELEVATION_MM
        points3 = [(round(x, 6), round(y, 6), round(z, 6)) for x, y in corners]
        lines.append(
            DrawnLine(name=f"Line {index + 1}", points=points3, orthogonal=is_orthogonal(points3))
        )
    if elevation is None:
        elevation_source = "default"
    pipe_z = elevation if elevation is not None else DEFAULT_PIPE_ELEVATION_MM
    assumptions.append(
        Assumption(
            quantity="Pipe centreline elevation",
            value=f"{pipe_z:g} mm",
            source=elevation_source,  # type: ignore[arg-type]
            note="Each run is checked at one elevation; risers shown only in elevations "
            "are not reconstructed.",
        )
    )
    if not lines:
        return InferredLayout(
            supported=False,
            message="Syntera found pipe layers but no continuous pipe run in the plan.",
            assumptions=assumptions,
            views=views,
        )

    if front is not None and options.equipment_height_mm is None:
        height = front[3] - front[1]
    else:
        tops = [
            o.center[2] + (o.size[2] / 2 if isinstance(o, BoxObstacle) else o.height / 2)
            for o in obstacles
        ]
        height = max([*tops, pipe_z + od / 2 + clearance]) + max(
            2 * (od / 2 + clearance), 0.1 * max([*tops, pipe_z])
        )
    return InferredLayout(
        supported=True,
        message=f"Read {len(obstacles)} equipment item(s) and {len(lines)} pipe run(s).",
        workspace=(round(extent[0], 6), round(extent[1], 6), round(height, 6)),
        obstacles=obstacles,
        equipment=equipment,
        lines=lines,
        tube=tube,
        assumptions=assumptions,
        views=views,
        rationale=tube_rationale(
            tube,
            od_source=_source(options.outer_diameter_mm, drawn_od),
            radius_source=_source(options.bend_radius_mm, drawn_radius),
            radius_clamped=radius != chosen_radius,
            clearance_source=_source(options.clearance_mm, drawn_clearance),
        ),
    )
