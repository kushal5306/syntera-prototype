"""Propose a skid layout for P&ID equipment: sizes, deterministic placement and nozzles.

Sizes come from P&ID attributes where present (tank length and capacity, tube length,
plate size, column height and diameter), otherwise from stated class defaults. Equipment
is placed on a grid and improved by pairwise swaps that shorten the straight-line pipe
length between connected nozzles; every step is deterministic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from syntera.pid.models import PidDocument, PidEquipment, PlacedEquipment, Point3, Terminal

ACCESS_GAP_MM = 1500.0
SKID_MARGIN_MM = 1500.0
HEADROOM_MM = 1000.0
MAX_SWAP_PASSES = 50
# ISO 4200 / ASME B36.10 outside diameters by nominal size.
OUTSIDE_DIAMETER_MM = {
    15: 21.3, 20: 26.9, 25: 33.7, 32: 42.4, 40: 48.3, 50: 60.3, 65: 76.1, 80: 88.9,
    100: 114.3, 125: 139.7, 150: 168.3, 200: 219.1, 250: 273.0, 300: 323.9, 350: 355.6,
    400: 406.4, 450: 457.0, 500: 508.0, 600: 610.0, 700: 711.0, 800: 813.0,
}  # fmt: skip
DEFAULT_DN = 50
# (shape, axis, length x, width y, height z) for classes without size attributes
CLASS_DEFAULTS: tuple[tuple[str, tuple[str, str, float, float, float]], ...] = (
    ("Column", ("cylinder", "z", 1500, 1500, 8000)),
    ("Tank", ("cylinder", "z", 2500, 2500, 3500)),
    ("Vessel", ("cylinder", "z", 1500, 1500, 2500)),
    ("Drum", ("cylinder", "x", 3000, 1200, 1200)),
    ("Separator", ("cylinder", "z", 1200, 1200, 2500)),
    ("Filter", ("cylinder", "z", 600, 600, 1200)),
    ("Compressor", ("box", "z", 2500, 1500, 1800)),
    ("Pump", ("box", "z", 1200, 600, 800)),
    ("PlateHeatExchanger", ("box", "z", 1200, 800, 1500)),
    ("HeatExchanger", ("cylinder", "x", 3000, 800, 800)),
    ("Mixer", ("cylinder", "z", 1200, 1200, 1800)),
)
GENERIC_DEFAULT = ("box", "z", 1000.0, 1000.0, 1000.0)


def outside_diameter(nominal: int | None) -> float:
    if nominal is None:
        return OUTSIDE_DIAMETER_MM[DEFAULT_DN]
    known = sorted(OUTSIDE_DIAMETER_MM)
    nearest = min(known, key=lambda size: (abs(size - nominal), size))
    return OUTSIDE_DIAMETER_MM[nearest]


@dataclass(frozen=True)
class Envelope:
    shape: str
    axis: str
    size: Point3
    source: str
    note: str
    rule_id: str | None = None  # the sizing rule, when the size is not simply read or assumed


def envelope(item: PidEquipment) -> Envelope:
    """Equipment size from P&ID attributes, else the class default (both stated)."""
    a = item.attributes
    name = item.component_class
    diameter = a.get("OutsideDiameter") or a.get("InsideDiameter")
    nominal = a.get("NominalDiameter")
    if nominal and nominal <= 6000:
        diameter = diameter or nominal
    if "Tank" in name or "Vessel" in name:
        length = a.get("CylinderLength") or a.get("Height")
        volume = a.get("NominalCapacity(Volume)")
        if length and volume and not diameter:
            diameter = math.sqrt(4 * volume / (math.pi * length))
        if length and diameter:
            note = f"{diameter:.0f} mm diameter x {length:.0f} mm from the P&ID"
            rule = None
            if volume and not (a.get("InsideDiameter") or a.get("OutsideDiameter")):
                note = (
                    f"{diameter:.0f} mm diameter from {volume / 1e9:g} m3 over "
                    f"{length:.0f} mm length"
                )
                rule = "pid.vessel_diameter_from_capacity.v1"
            return Envelope("cylinder", "z", (diameter, diameter, length), "pid", note, rule)
    if "Column" in name and a.get("Height") and diameter:
        height = a["Height"]
        return Envelope(
            "cylinder", "z", (diameter, diameter, height), "pid",
            f"{diameter:.0f} mm diameter x {height:.0f} mm tall from the P&ID",
        )  # fmt: skip
    if "TubularHeatExchanger" in name or "ShellAndTube" in name:
        length = (a.get("TubeLength") or 2500) + 600
        diameter = diameter or 600
        source = "pid" if a.get("TubeLength") else "default"
        return Envelope(
            "cylinder", "x", (length, diameter, diameter), source,
            f"shell {diameter:.0f} mm x {length:.0f} mm (tube length + 600 mm heads)",
            "pid.shell_from_tube_length.v1",
        )  # fmt: skip
    if "PlateHeatExchanger" in name and a.get("PlateHeight"):
        width, height = a.get("PlateWidth", 800), a["PlateHeight"]
        return Envelope(
            "box", "z", (width + 400, 800, height + 300), "pid",
            "plate size from the P&ID plus frame; 800 mm pack depth assumed",
            "pid.plate_frame_allowance.v1",
        )  # fmt: skip
    for word, (shape, axis, x, y, z) in CLASS_DEFAULTS:
        if word in name:
            return Envelope(
                shape, axis, (x, y, z), "default", f"{word} class default",
                "pid.class_default_envelope.v1",
            )  # fmt: skip
    shape, axis, x, y, z = GENERIC_DEFAULT
    return Envelope(
        shape, axis, (x, y, z), "default", "generic 1 m cube", "pid.class_default_envelope.v1"
    )


@dataclass(frozen=True)
class NozzlePort:
    """Where a nozzle sits on its equipment and the routing start point just outside it."""

    terminal_id: str
    face: Point3  # flange point on the equipment surface
    port: Point3  # first routed point, clear of the equipment
    direction: Point3  # outward unit vector (cardinal)


@dataclass(frozen=True)
class Layout:
    equipment: list[PlacedEquipment]
    workspace: Point3
    ports: dict[str, NozzlePort]
    score_mm: float


def _snap(value: float, step: float) -> float:
    return round(value / step) * step


def _cells(count: int) -> list[tuple[int, int]]:
    columns = max(1, math.ceil(math.sqrt(count)))
    rows = max(1, math.ceil(count / columns))
    return [(row, column) for row in range(rows) for column in range(columns)]


def place(document: PidDocument, step: float) -> Layout:
    """Place equipment on a grid of equal cells, then swap cells to shorten connections."""
    items = sorted(document.equipment, key=lambda e: e.tag)
    envelopes = {e.id: envelope(e) for e in items}
    cell_x = max((env.size[0] for env in envelopes.values()), default=1000) + ACCESS_GAP_MM
    cell_y = max((env.size[1] for env in envelopes.values()), default=1000) + ACCESS_GAP_MM
    cell_x, cell_y = math.ceil(cell_x / step) * step, math.ceil(cell_y / step) * step
    cells = _cells(len(items))
    # one extra column of free cells lets the swap search open gaps where it helps
    if len(cells) == len(items) and len(items) > 1:
        columns = max(c for _, c in cells) + 1
        cells += [(row, columns) for row in range(max(r for r, _ in cells) + 1)]
    pairs = [
        (c.start.equipment_id, c.end.equipment_id)
        for c in document.connections
        if c.start.equipment_id and c.end.equipment_id
    ]
    edge_links = [
        (
            c.start if c.start.kind == "nozzle" else c.end,
            c.start if c.start.kind != "nozzle" else c.end,
        )
        for c in document.connections
        if (c.start.kind == "nozzle") != (c.end.kind == "nozzle")
    ]
    rows = max((r for r, _ in cells), default=0) + 1
    columns = max((c for _, c in cells), default=0) + 1
    width = columns * cell_x + 2 * SKID_MARGIN_MM

    def centre(cell: tuple[int, int]) -> tuple[float, float]:
        return (
            SKID_MARGIN_MM + (cell[1] + 0.5) * cell_x,
            SKID_MARGIN_MM + (cell[0] + 0.5) * cell_y,
        )

    def score(assign: dict[str, tuple[int, int]]) -> float:
        total = 0.0
        for a, b in pairs:
            (ax, ay), (bx, by) = centre(assign[a]), centre(assign[b])
            total += abs(ax - bx) + abs(ay - by)
        for nozzle, limit in edge_links:
            x, _ = centre(assign[nozzle.equipment_id])  # type: ignore[index]
            total += x if limit.inbound else width - x
        return total

    assign = {item.id: cells[index] for index, item in enumerate(items)}
    best = score(assign)
    for _ in range(MAX_SWAP_PASSES):
        improved = False
        for item in items:
            for cell in cells:
                if cell == assign[item.id]:
                    continue
                other = next((k for k, v in assign.items() if v == cell), None)
                trial = dict(assign)
                trial[item.id] = cell
                if other is not None:
                    trial[other] = assign[item.id]
                value = score(trial)
                if value + 1e-6 < best:
                    assign, best, improved = trial, value, True
        if not improved:
            break

    used_rows = sorted({r for r, _ in assign.values()}) or [0]
    used_columns = sorted({c for _, c in assign.values()}) or [0]
    row_index = {r: i for i, r in enumerate(used_rows)}
    column_index = {c: i for i, c in enumerate(used_columns)}
    assign = {k: (row_index[r], column_index[c]) for k, (r, c) in assign.items()}
    rows, columns = len(used_rows), len(used_columns)
    largest = max(
        (outside_diameter(c.nominal_diameter) for c in document.connections), default=60.3
    )
    height = max(
        max((env.size[2] for env in envelopes.values()), default=0.0) + HEADROOM_MM,
        2 * _stand_off(largest, step) + step,
    )
    workspace = (
        columns * cell_x + 2 * SKID_MARGIN_MM,
        rows * cell_y + 2 * SKID_MARGIN_MM,
        math.ceil(height / step) * step,
    )
    placed: list[PlacedEquipment] = []
    for item in items:
        env = envelopes[item.id]
        x, y = centre(assign[item.id])
        z0 = 300.0 if env.axis == "x" else 0.0  # horizontal shells sit on saddles
        placed.append(
            PlacedEquipment(
                tag=item.tag,
                component_class=item.component_class,
                shape=env.shape,  # type: ignore[arg-type]
                axis=env.axis,  # type: ignore[arg-type]
                center=(_snap(x, step), _snap(y, step), z0 + env.size[2] / 2),
                size=env.size,
                size_source=env.source,  # type: ignore[arg-type]
                size_note=env.note,
            )
        )
    by_id = {item.id: p for item, p in zip(items, placed, strict=True)}
    ports = _nozzle_ports(document, by_id, workspace, step)
    return Layout(placed, workspace, ports, best)


def _partner(document: PidDocument, terminal_id: str) -> Terminal | None:
    for connection in document.connections:
        if connection.start.id == terminal_id:
            return connection.end
        if connection.end.id == terminal_id:
            return connection.start
    return None


def clearance_for(outer_diameter: float) -> float:
    """Default clearance between a pipe and anything else: half its OD, at least 25 mm."""
    return max(25.0, outer_diameter / 2)


def _stand_off(outer_diameter: float, step: float) -> float:
    """Grid distance from a surface at which a pipe of this size is clear to start routing."""
    needed = outer_diameter / 2 + clearance_for(outer_diameter)
    return (math.floor(needed / step) + 1) * step


def _nozzle_ports(
    document: PidDocument,
    placed: dict[str, PlacedEquipment],
    workspace: Point3,
    step: float,
) -> dict[str, NozzlePort]:
    """Put each connected nozzle on the face looking towards what it connects to.

    Nozzles sharing a face are spread along it two grid steps apart. Battery limits sit on
    the west (inlets) or east (outlets) skid edge level with the nozzle they connect to.
    Every port direction points from its terminal into the space the pipe is routed in.
    """
    ods = {}
    for connection in document.connections:
        od = outside_diameter(connection.nominal_diameter)
        for end in (connection.start, connection.end):
            ods[end.id] = max(ods.get(end.id, 0.0), od)
    ports: dict[str, NozzlePort] = {}
    faces: dict[tuple[str, Point3], list[str]] = {}
    targets: dict[str, tuple[float, float]] = {}
    for connection in document.connections:
        for here, there in ((connection.start, connection.end), (connection.end, connection.start)):
            if here.kind != "nozzle" or here.id in targets:
                continue
            owner = placed[here.equipment_id]  # type: ignore[index]
            if there.kind == "nozzle":
                target = placed[there.equipment_id].center  # type: ignore[index]
                tx, ty = target[0], target[1]
            else:
                tx, ty = (0.0 if there.inbound else workspace[0]), owner.center[1]
            targets[here.id] = (tx, ty)
            dx, dy = tx - owner.center[0], ty - owner.center[1]
            if abs(dx) >= abs(dy):
                direction = (math.copysign(1.0, dx or 1.0), 0.0, 0.0)
            else:
                direction = (0.0, math.copysign(1.0, dy), 0.0)
            faces.setdefault((here.equipment_id, direction), []).append(here.id)  # type: ignore[arg-type]

    for (equipment_id, direction), nozzles in sorted(faces.items()):
        owner = placed[equipment_id]
        axis = 0 if direction[0] else 1
        tangent = 1 - axis
        half = owner.size[axis] / 2
        nozzles.sort(key=lambda n: (targets[n][tangent], n))
        spacing = 2 * step
        usable = max(1, min(len(nozzles), int((owner.size[tangent] - step) // spacing) + 1))
        for index, nozzle in enumerate(nozzles):
            offset = ((index % usable) - (usable - 1) / 2) * spacing
            base = owner.center[2] - owner.size[2] / 2
            z = _snap(base + min(owner.size[2] / 2, 800.0) + (index // usable) * spacing, step)
            face = [0.0, 0.0, z]
            face[axis] = owner.center[axis] + direction[axis] * half
            face[tangent] = _snap(owner.center[tangent] + offset, step)
            port = list(face)
            reach = face[axis] + direction[axis] * _stand_off(ods[nozzle], step)
            port[axis] = (
                math.ceil(reach / step) * step if direction[axis] > 0
                else math.floor(reach / step) * step
            )  # fmt: skip
            ports[nozzle] = NozzlePort(nozzle, tuple(face), tuple(port), direction)  # type: ignore[arg-type]

    used: set[Point3] = {p.port for p in ports.values()}
    for connection in document.connections:
        for here, there in ((connection.start, connection.end), (connection.end, connection.start)):
            if here.kind != "battery_limit" or here.id in ports:
                continue
            level = ports[there.id].port if there.id in ports else None
            y = level[1] if level else _snap(workspace[1] / 2, step)
            inset = _stand_off(ods[here.id], step)
            z = level[2] if level else max(_snap(min(workspace[2] / 2, 800.0), step), inset)
            x = inset if here.inbound else math.floor((workspace[0] - inset) / step) * step
            while (x, y, z) in used:
                y += 2 * step
            used.add((x, y, z))
            edge = 0.0 if here.inbound else workspace[0]
            direction = (1.0, 0.0, 0.0) if here.inbound else (-1.0, 0.0, 0.0)
            ports[here.id] = NozzlePort(here.id, (edge, y, z), (x, y, z), direction)
    return ports
