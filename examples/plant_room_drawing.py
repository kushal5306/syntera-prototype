"""Draw a small solvent recovery plant room as a piping general arrangement (GA) DXF.

The drawing follows common piping GA practice rather than Syntera's own layout format, so it
is a fair test of what Syntera can read from a real drawing:

* National CAD Standard style layers (P-EQPM, P-PIPE-PROC, P-VALV, S-COLS, A-WALL, ...).
* Single-line pipe centrelines with line numbers (``100-P-2001-A1A``: DN, service, number,
  pipe class) and centreline elevations (``CL EL +4.200``, metres) beside each run.
* Lines change level at risers; nozzles on vessel tops and bottoms are reached by a riser
  that is only implied in plan (the run ends inside the vessel outline).
* Equipment as circles, polylines and blocks (pumps), tagged ``T-201``, ``P-201A`` ...
* A plan and a SECTION A-A below it at the same scale, view titles under each view.
* Valves and instruments as blocks, tees, battery limits at the walls, a drain hub, grid
  lines, dimensions and a title block.

Like most real drawings it has a few mistakes for a checker to find: the overhead vapour
line 150-P-2010 runs at the same level as the pump discharge 80-P-2003 where they cross, and
the condensate line 80-P-2011 leaves the condenser's bottom nozzle with too little drop for
its elbow. Syntera also found one the author did not intend: both cooling water lines pass
through the wall exactly where building column C-2 stands.

All coordinates are millimetres (``$INSUNITS`` = 4). Run ``python examples/plant_room_drawing.py
out.dxf`` to write it.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import ezdxf
from ezdxf.document import Drawing
from ezdxf.enums import TextEntityAlignment
from ezdxf.layouts import Modelspace

ROOM = (14000.0, 10000.0)  # inside faces of the walls
WALL = 200.0
ROOF = 6000.0
SECTION_FLOOR_Y = -9000.0  # section A-A is drawn below the plan, X aligned with it
TEXT = 150.0
DIM_STYLE = {"dimtxt": 150, "dimasz": 120, "dimlfac": 1, "dimdec": 0}

LAYERS = {
    "A-WALL": 8,
    "S-GRID": 9,
    "S-COLS": 8,
    "S-STEEL": 8,
    "P-EQPM": 4,
    "P-EQPM-NOZL": 4,
    "P-PIPE-PROC": 1,
    "P-PIPE-UTIL": 3,
    "P-VALV": 2,
    "P-INST": 6,
    "P-ANNO-LINE": 7,
    "P-ANNO-ELEV": 7,
    "P-ANNO-EQPT": 7,
    "P-ANNO-DIMS": 7,
    "P-ANNO-NOTE": 7,
    "G-ANNO-TTLB": 7,
}

# (tag, description, plan shape, z bottom, z top)
#  shape: ("circle", cx, cy, diameter) | ("rect", x0, y0, x1, y1) | ("pump", x0, y0, x1, y1)
EQUIPMENT = [
    ("T-201", "CRUDE SOLVENT TANK", ("circle", 2200, 7600, 2400), 300, 4300),
    ("T-202", "RECOVERED SOLVENT TANK", ("circle", 2200, 2600, 2000), 300, 3800),
    ("P-201A", "TRANSFER PUMP", ("pump", 4200, 6600, 5600, 7200), 0, 900),
    ("P-201B", "TRANSFER PUMP", ("pump", 4200, 5200, 5600, 5800), 0, 900),
    ("R-210", "SOLVENT STILL", ("circle", 9000, 7600, 1800), 500, 3700),
    ("E-220", "OVERHEAD CONDENSER", ("rect", 9800, 3300, 13400, 4000), 2400, 3100),
    ("V-230", "DISTILLATE RECEIVER", ("rect", 10000, 1200, 12400, 2100), 800, 1800),
    ("P-230", "REFLUX / PRODUCT PUMP", ("pump", 7600, 1300, 8600, 1800), 0, 800),
]

# Structural columns (400 x 400) at the grid intersections; the middle one stands free.
COLUMNS = [(x, y) for x in (0, 7000, 14000) for y in (0, 5000, 10000)]

# Condenser support posts (200 x 200, floor to the condenser): (x, y, top)
STEEL_POSTS = [(10300, 3650, 2400), (12900, 3650, 2400)]

# (line number, layer, [(x, y), ...], centreline elevation in mm, or {leg index: elevation})
# fmt: off
LINES = [
    (
        "100-P-2001-A1A",
        "P-PIPE-PROC",
        [(3400, 7600), (3800, 7600), (3800, 5500), (4200, 5500)],
        600,
    ),
    ("100-P-2002-A1A", "P-PIPE-PROC", [(3800, 6900), (4200, 6900)], 600),
    (
        "80-P-2003-A1A",
        "P-PIPE-PROC",
        [(5000, 6900), (5000, 9000), (9600, 9000), (9600, 7400)],
        4200,
    ),
    ("80-P-2004-A1A", "P-PIPE-PROC", [(5000, 5500), (5800, 5500), (5800, 9000)], 4200),
    ("150-P-2010-B1A", "P-PIPE-PROC", [(9400, 7600), (10200, 7600), (10200, 3650)], 4200),
    ("80-P-2011-B1A", "P-PIPE-PROC", [(13000, 3650), (13000, 1650), (12000, 1650)], 2200),
    ("80-P-2012-A1A", "P-PIPE-PROC", [(10400, 1550), (8600, 1550)], 450),
    ("50-P-2013-A1A", "P-PIPE-PROC", [(7800, 1550), (7800, 7600), (8100, 7600)], 3500),
    ("50-P-2014-A1A", "P-PIPE-PROC", [(7800, 2600), (3200, 2600)], 3500),
    ("80-P-2020-A1A", "P-PIPE-PROC", [(9000, 7300), (9000, 6000), (7400, 6000)], 200),
    ("50-CW-2030-C1A", "P-PIPE-UTIL", [(14000, 4800), (13200, 4800), (13200, 3650)], 3500),
    ("50-CW-2031-C1A", "P-PIPE-UTIL", [(12800, 3650), (12800, 5200), (14000, 5200)], 3500),
    ("25-N-2040-N1A", "P-PIPE-UTIL", [(1600, 10000), (1600, 8000)], 4600),
    (
        "25-N-2041-N1A",
        "P-PIPE-UTIL",
        [(1600, 9400), (600, 9400), (600, 3000), (1600, 3000)],
        {1: 4600, 2: 4400},
    ),
]

# (block, x, y, rotation degrees, tag)
VALVES = [
    ("GATE_VALVE", 3600, 7600, 0, "XV-2001"),
    ("GATE_VALVE", 3800, 6200, 90, ""),
    ("GATE_VALVE", 5000, 7800, 90, ""),
    ("CHECK_VALVE", 5000, 8300, 90, ""),
    ("GATE_VALVE", 5800, 7000, 90, ""),
    ("CHECK_VALVE", 5800, 7600, 90, ""),
    ("GLOBE_VALVE", 7800, 4300, 90, "FCV-2013"),
    ("GATE_VALVE", 5500, 2600, 0, ""),
    ("GATE_VALVE", 9500, 1550, 0, ""),
    ("GATE_VALVE", 9000, 6600, 90, ""),
    ("GATE_VALVE", 13600, 4800, 0, ""),
    ("GATE_VALVE", 13400, 5200, 0, ""),
    ("GLOBE_VALVE", 600, 5000, 90, "PCV-2040"),
]

INSTRUMENTS = [("PI", "2003", 5300, 8600), ("TI", "2010", 10500, 6200), ("FT", "2013", 8150, 4700)]


def _layers(doc: Drawing) -> None:
    for name, colour in LAYERS.items():
        doc.layers.add(name, color=colour)
    doc.layers.get("S-GRID").dxf.linetype = "CENTER"
    doc.layers.get("P-ANNO-DIMS").dxf.color = 7


def _blocks(doc: Drawing) -> None:
    gate = doc.blocks.new("GATE_VALVE")
    gate.add_lwpolyline([(-150, -100), (150, 100), (150, -100), (-150, 100)], close=True)
    globe = doc.blocks.new("GLOBE_VALVE")
    globe.add_lwpolyline([(-150, -100), (150, 100), (150, -100), (-150, 100)], close=True)
    globe.add_circle((0, 0), 50)
    check = doc.blocks.new("CHECK_VALVE")
    check.add_lwpolyline([(-150, -100), (150, 100), (150, -100), (-150, 100)], close=True)
    check.add_line((-150, 100), (-150, -100))
    check.add_line((0, -120), (120, 0))
    bubble = doc.blocks.new("INSTRUMENT")
    bubble.add_circle((0, 0), 250)
    bubble.add_line((-250, 0), (250, 0))
    riser = doc.blocks.new("DRAIN_HUB")
    riser.add_circle((0, 0), 150)
    riser.add_circle((0, 0), 75)


def _pump(msp: Modelspace, tag: str, x0: float, y0: float, x1: float, y1: float) -> None:
    """A centrifugal pump set as a block: baseplate, casing, coupling guard and motor."""
    doc = msp.doc
    name = f"PUMP_{tag}"
    block = doc.blocks.new(name)
    width, depth = x1 - x0, y1 - y0
    block.add_lwpolyline([(0, 0), (width, 0), (width, depth), (0, depth)], close=True)
    block.add_circle((depth * 0.6, depth / 2), depth * 0.38)  # volute casing
    block.add_lwpolyline(
        [(depth * 1.0, depth * 0.38), (depth * 1.4, depth * 0.38),
         (depth * 1.4, depth * 0.62), (depth * 1.0, depth * 0.62)], close=True,
    )  # fmt: skip
    block.add_lwpolyline(
        [(depth * 1.45, depth * 0.15), (width - 50, depth * 0.15),
         (width - 50, depth * 0.85), (depth * 1.45, depth * 0.85)], close=True,
    )  # fmt: skip
    msp.add_blockref(name, (x0, y0), dxfattribs={"layer": "P-EQPM"})


def _text(
    msp: Modelspace,
    text: str,
    at: tuple[float, float],
    layer: str,
    height: float = TEXT,
    rotation: float = 0.0,
    align: TextEntityAlignment = TextEntityAlignment.MIDDLE_CENTER,
) -> None:
    msp.add_text(
        text, height=height, rotation=rotation, dxfattribs={"layer": layer, "style": "Standard"}
    ).set_placement(at, align=align)


def _plan(msp: Modelspace) -> None:
    width, depth = ROOM
    # walls: outer and inner faces
    msp.add_lwpolyline(
        [(-WALL, -WALL), (width + WALL, -WALL), (width + WALL, depth + WALL),
         (-WALL, depth + WALL)], close=True, dxfattribs={"layer": "A-WALL"},
    )  # fmt: skip
    msp.add_lwpolyline(
        [(0, 0), (width, 0), (width, depth), (0, depth)], close=True, dxfattribs={"layer": "A-WALL"}
    )
    # door opening in the south wall
    msp.add_line((5600, -WALL), (5600, 0), dxfattribs={"layer": "A-WALL"})
    msp.add_line((6600, -WALL), (6600, 0), dxfattribs={"layer": "A-WALL"})
    msp.add_arc((5600, 0), 1000, 0, 90, dxfattribs={"layer": "A-WALL"})
    # grid and columns
    for label, x in zip("ABC", (0, 7000, 14000), strict=True):
        msp.add_line((x, -1200), (x, depth + 1200), dxfattribs={"layer": "S-GRID"})
        msp.add_circle((x, depth + 1500), 300, dxfattribs={"layer": "S-GRID"})
        _text(msp, label, (x, depth + 1500), "S-GRID", 250)
    for label, y in zip("123", (0, 5000, 10000), strict=True):
        msp.add_line((-1200, y), (width + 1200, y), dxfattribs={"layer": "S-GRID"})
        msp.add_circle((-1500, y), 300, dxfattribs={"layer": "S-GRID"})
        _text(msp, label, (-1500, y), "S-GRID", 250)
    for x, y in COLUMNS:
        msp.add_lwpolyline(
            [(x - 200, y - 200), (x + 200, y - 200), (x + 200, y + 200), (x - 200, y + 200)],
            close=True, dxfattribs={"layer": "S-COLS"},
        )  # fmt: skip
    # equipment
    for tag, description, shape, _, top in EQUIPMENT:
        if shape[0] == "circle":
            _, cx, cy, diameter = shape
            msp.add_circle((cx, cy), diameter / 2, dxfattribs={"layer": "P-EQPM"})
            centre = (cx, cy)
        elif shape[0] == "rect":
            _, x0, y0, x1, y1 = shape
            msp.add_lwpolyline(
                [(x0, y0), (x1, y0), (x1, y1), (x0, y1)], close=True,
                dxfattribs={"layer": "P-EQPM"},
            )  # fmt: skip
            # dished heads drawn as arcs inside the outline
            centre = ((x0 + x1) / 2, (y0 + y1) / 2)
        else:
            _, x0, y0, x1, y1 = shape
            _pump(msp, tag, x0, y0, x1, y1)
            centre = ((x0 + x1) / 2, (y0 + y1) / 2)
        _text(msp, tag, (centre[0], centre[1] - 60), "P-ANNO-EQPT", 200)
        if shape[0] != "pump":
            _text(msp, description, (centre[0], centre[1] + 220), "P-ANNO-EQPT", 100)
        _text(msp, f"TOP EL +{top / 1000:.3f}", (centre[0], centre[1] - 300), "P-ANNO-EQPT", 90)
    # condenser support steel (under the condenser, hidden in plan)
    for x, y, _ in STEEL_POSTS:
        msp.add_lwpolyline(
            [(x - 100, y - 100), (x + 100, y - 100), (x + 100, y + 100), (x - 100, y + 100)],
            close=True, dxfattribs={"layer": "S-STEEL", "linetype": "HIDDEN"},
        )  # fmt: skip
    # pipes, line numbers and elevations
    for number, layer, points, elevation in LINES:
        msp.add_lwpolyline(points, dxfattribs={"layer": layer})
        legs = list(zip(points, points[1:], strict=False))
        longest = max(range(len(legs)), key=lambda k: math.dist(*legs[k]))
        levels = elevation if isinstance(elevation, dict) else {longest: elevation}
        for k in sorted({longest, *levels}):
            a, b = legs[k]
            mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            horizontal = abs(b[1] - a[1]) < 1e-6
            angle = 0.0 if horizontal else 90.0
            normal = (0.0, 1.0) if horizontal else (-1.0, 0.0)
            if k == longest:
                _text(
                    msp, number, (mid[0] + normal[0] * 150, mid[1] + normal[1] * 150),
                    "P-ANNO-LINE", 110, angle, TextEntityAlignment.BOTTOM_CENTER,
                )  # fmt: skip
            if k in levels:
                _text(
                    msp, f"CL EL +{levels[k] / 1000:.3f}",
                    (mid[0] - normal[0] * 150, mid[1] - normal[1] * 150), "P-ANNO-ELEV",
                    90, angle, TextEntityAlignment.TOP_CENTER,
                )  # fmt: skip
    # nitrogen supply and cooling water battery limits at the walls
    _text(msp, "N2 SUPPLY FROM UTILITY RACK", (1600, depth + 600), "P-ANNO-NOTE", 100)
    _text(msp, "CWS / CWR B.L.", (width + 700, 5000), "P-ANNO-NOTE", 100, 90)
    msp.add_blockref("DRAIN_HUB", (7400, 6000), dxfattribs={"layer": "P-EQPM-NOZL"})
    _text(msp, "TO CLOSED DRAIN DH-01", (7400, 5650), "P-ANNO-NOTE", 90)
    # valves and instruments
    for block, x, y, rotation, tag in VALVES:
        msp.add_blockref(block, (x, y), dxfattribs={"layer": "P-VALV", "rotation": rotation})
        if tag:
            _text(msp, tag, (x + 350, y + 250), "P-VALV", 90)
    for function, number, x, y in INSTRUMENTS:
        msp.add_blockref("INSTRUMENT", (x, y), dxfattribs={"layer": "P-INST"})
        _text(msp, function, (x, y + 100), "P-INST", 100)
        _text(msp, number, (x, y - 100), "P-INST", 100)
    # section cut and dimensions
    msp.add_line((-900, 4400), (-500, 4400), dxfattribs={"layer": "P-ANNO-NOTE"})
    msp.add_line((width + 500, 4400), (width + 900, 4400), dxfattribs={"layer": "P-ANNO-NOTE"})
    _text(msp, "A", (-700, 4650), "P-ANNO-NOTE", 250)
    _text(msp, "A", (width + 700, 4650), "P-ANNO-NOTE", 250)
    for x0, x1 in ((0, 7000), (7000, 14000)):
        msp.add_linear_dim(
            base=(0, depth + 900), p1=(x0, depth), p2=(x1, depth),
            dxfattribs={"layer": "P-ANNO-DIMS"}, override=DIM_STYLE,
        ).render()  # fmt: skip
    msp.add_linear_dim(
        base=(width + 1100, 0), p1=(width, 0), p2=(width, depth), angle=90,
        dxfattribs={"layer": "P-ANNO-DIMS"}, override=DIM_STYLE,
    ).render()  # fmt: skip
    _text(msp, "PLAN", (width / 2, -900), "P-ANNO-NOTE", 300)
    _text(msp, "SCALE 1:50", (width / 2, -1250), "P-ANNO-NOTE", 150)


def _section(msp: Modelspace) -> None:
    """Section A-A looking north: same X as the plan, Z measured up from the floor line."""
    width = ROOM[0]
    base = SECTION_FLOOR_Y

    def z(value: float) -> float:
        return base + value

    msp.add_lwpolyline(
        [(-WALL, z(0)), (width + WALL, z(0)), (width + WALL, z(ROOF)), (-WALL, z(ROOF))],
        close=True, dxfattribs={"layer": "A-WALL"},
    )  # fmt: skip
    msp.add_line((0, z(0)), (0, z(ROOF)), dxfattribs={"layer": "A-WALL"})
    msp.add_line((width, z(0)), (width, z(ROOF)), dxfattribs={"layer": "A-WALL"})
    msp.add_lwpolyline(
        [(6800, z(0)), (7200, z(0)), (7200, z(ROOF)), (6800, z(ROOF))], close=True,
        dxfattribs={"layer": "S-COLS"},
    )  # fmt: skip
    for tag, _, shape, bottom, top in EQUIPMENT:
        if shape[0] == "circle":
            x0, x1 = shape[1] - shape[3] / 2, shape[1] + shape[3] / 2
        else:
            x0, x1 = shape[1], shape[3]
        msp.add_lwpolyline(
            [(x0, z(bottom)), (x1, z(bottom)), (x1, z(top)), (x0, z(top))], close=True,
            dxfattribs={"layer": "P-EQPM"},
        )  # fmt: skip
        _text(msp, tag, ((x0 + x1) / 2, z((bottom + top) / 2)), "P-ANNO-EQPT", 200)
        if bottom > 0 and tag != "E-220":
            for leg in (x0 + 150, x1 - 150):
                msp.add_line((leg, z(0)), (leg, z(bottom)), dxfattribs={"layer": "S-STEEL"})
        _text(msp, f"EL +{top / 1000:.3f}", (x1 + 80, z(top)), "P-ANNO-ELEV", 90, 0,
              TextEntityAlignment.MIDDLE_LEFT)  # fmt: skip
    for x, _, top in STEEL_POSTS:
        msp.add_lwpolyline(
            [(x - 100, z(0)), (x + 100, z(0)), (x + 100, z(top)), (x - 100, z(top))], close=True,
            dxfattribs={"layer": "S-STEEL"},
        )  # fmt: skip
    for _, layer, points, elevation in LINES:
        xs = [p[0] for p in points]
        for level in elevation.values() if isinstance(elevation, dict) else [elevation]:
            msp.add_line((min(xs), z(level)), (max(xs), z(level)), dxfattribs={"layer": layer})
    _text(msp, "FFL EL +0.000", (-WALL - 100, z(0)), "P-ANNO-ELEV", 120, 0,
          TextEntityAlignment.MIDDLE_RIGHT)  # fmt: skip
    _text(msp, "SECTION A-A", (width / 2, z(0) - 700), "P-ANNO-NOTE", 300)


def _title_block(msp: Modelspace) -> None:
    x0, y0, x1, y1 = -3000, SECTION_FLOOR_Y - 2500, ROOM[0] + 3000, ROOM[1] + 3000
    msp.add_lwpolyline(
        [(x0, y0), (x1, y0), (x1, y1), (x0, y1)], close=True, dxfattribs={"layer": "G-ANNO-TTLB"}
    )
    msp.add_lwpolyline(
        [(x1 - 8600, y0), (x1, y0), (x1, y0 + 1600), (x1 - 8600, y0 + 1600)], close=True,
        dxfattribs={"layer": "G-ANNO-TTLB"},
    )  # fmt: skip
    rows = [
        "SOLVENT RECOVERY UNIT - AREA 200 PUMP ROOM",
        "PIPING GENERAL ARRANGEMENT - PLAN AND SECTION A-A",
        "DWG 200-PI-GA-001 REV B  SCALE 1:50  MM, EL IN M",
        "SYNTHETIC TEST DRAWING - NOT FOR CONSTRUCTION",
    ]
    for index, row in enumerate(rows):
        _text(msp, row, (x1 - 8400, y0 + 1300 - index * 330), "G-ANNO-TTLB", 140, 0,
              TextEntityAlignment.MIDDLE_LEFT)  # fmt: skip
    notes = [
        "NOTES:",
        "1. LINE NUMBERS: DN-SERVICE-NUMBER-PIPE CLASS. A1A/B1A CS 150#, C1A CW, N1A N2.",
        "2. ELEVATIONS ARE TO PIPE CENTRELINE (CL EL) UNLESS NOTED, FFL = EL +0.000.",
        "3. ALL ELBOWS LONG RADIUS (1.5D). MIN CLEARANCE 25 MM TO INSULATION.",
        "4. VERTICAL LEGS TO VESSEL TOP / BOTTOM NOZZLES ARE NOT SHOWN IN PLAN.",
    ]
    for index, row in enumerate(notes):
        _text(msp, row, (x0 + 300, y0 + 1300 - index * 260), "P-ANNO-NOTE", 120, 0,
              TextEntityAlignment.MIDDLE_LEFT)  # fmt: skip


def build(path: Path) -> Path:
    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = 4
    doc.header["$MEASUREMENT"] = 1
    _layers(doc)
    _blocks(doc)
    msp = doc.modelspace()
    _plan(msp)
    _section(msp)
    _title_block(msp)
    doc.saveas(path)
    return path


if __name__ == "__main__":
    build(Path(sys.argv[1] if len(sys.argv) > 1 else "plant-room-piping-ga.dxf"))
