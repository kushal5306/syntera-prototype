"""General-arrangement DXF of a routed skid: plan, front and side views in millimetres.

The drawing is a deliverable for review, not evidence: it is drawn from the same route and
configuration the assurance report checks, and it states that report's verdict verbatim.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import ezdxf
from ezdxf import units
from ezdxf.enums import TextEntityAlignment
from ezdxf.layouts import Modelspace

from syntera.geometry.centreline import ArcSegment, centreline_segments
from syntera.geometry.step_import import import_step_obstacle
from syntera.schemas import (
    AssuranceReport,
    BoxObstacle,
    CylinderObstacle,
    DemoConfig,
    Point3,
    RouteResult,
    StepObstacle,
)

ARC_STEPS = 12
LAYERS = {
    "FRAME": 8,  # grey: workspace limits
    "EQUIPMENT": 1,  # red
    "PIPE": 141,  # light blue: tube drawn at its outer diameter
    "PIPE-CL": 4,  # cyan: centreline
    "NOZZLE": 3,  # green: start/end ports
    "DIMENSION": 2,  # yellow
    "TEXT": 7,
    "TITLE": 7,
}

Point2 = tuple[float, float]


@dataclass(frozen=True)
class View:
    """One orthographic view: which model axes map to sheet x/y, and where it sits."""

    title: str
    axes: tuple[int, int]
    origin: Point2

    @property
    def normal_axis(self) -> int:
        return ({0, 1, 2} - set(self.axes)).pop()

    def project(self, point: Point3) -> Point2:
        return (
            self.origin[0] + point[self.axes[0]],
            self.origin[1] + point[self.axes[1]],
        )


def _views(dimensions: tuple[float, float, float], gap: float) -> list[View]:
    """Third-angle arrangement: plan above the front elevation, right elevation beside it."""
    width, _, height = dimensions
    return [
        View("PLAN", (0, 1), (0.0, height + gap)),
        View("FRONT ELEVATION (LOOKING +Y)", (0, 2), (0.0, 0.0)),
        View("RIGHT ELEVATION (LOOKING -X)", (1, 2), (width + gap, 0.0)),
    ]


def _rectangle(msp: Modelspace, lower: Point2, upper: Point2, layer: str) -> None:
    corners = [lower, (upper[0], lower[1]), upper, (lower[0], upper[1])]
    msp.add_lwpolyline(corners, close=True, dxfattribs={"layer": layer})


def _box_outline(msp: Modelspace, view: View, lower: Point3, upper: Point3, layer: str) -> Point2:
    low, high = view.project(lower), view.project(upper)
    _rectangle(msp, low, high, layer)
    return ((low[0] + high[0]) / 2, (low[1] + high[1]) / 2)


def _cylinder_outline(msp: Modelspace, view: View, cylinder: CylinderObstacle) -> Point2:
    axis = "xyz".index(cylinder.axis)
    centre = view.project(cylinder.center)
    if axis == view.normal_axis:
        msp.add_circle(centre, cylinder.radius, dxfattribs={"layer": "EQUIPMENT"})
        return centre
    half = [cylinder.radius] * 3
    half[axis] = cylinder.height / 2
    lower = tuple(c - h for c, h in zip(cylinder.center, half, strict=True))
    upper = tuple(c + h for c, h in zip(cylinder.center, half, strict=True))
    return _box_outline(msp, view, lower, upper, "EQUIPMENT")


def _equipment(msp: Modelspace, view: View, config: DemoConfig, text_height: float) -> None:
    for obstacle in config.obstacles:
        if isinstance(obstacle, BoxObstacle):
            half = [size / 2 for size in obstacle.size]
            lower = tuple(c - h for c, h in zip(obstacle.center, half, strict=True))
            upper = tuple(c + h for c, h in zip(obstacle.center, half, strict=True))
            centre = _box_outline(msp, view, lower, upper, "EQUIPMENT")
        elif isinstance(obstacle, CylinderObstacle):
            centre = _cylinder_outline(msp, view, obstacle)
        elif isinstance(obstacle, StepObstacle):
            lower, upper = import_step_obstacle(obstacle).summary.routing_bounding_box_mm
            centre = _box_outline(msp, view, tuple(lower), tuple(upper), "EQUIPMENT")
        else:  # pragma: no cover - the schema union is closed
            raise TypeError(f"unsupported obstacle {type(obstacle).__name__}")
        _label(msp, obstacle.name.upper(), centre, text_height * 0.6)


def _label(msp: Modelspace, text: str, at: Point2, height: float, layer: str = "TEXT") -> None:
    msp.add_text(text, height=height, dxfattribs={"layer": layer}).set_placement(
        at, align=TextEntityAlignment.MIDDLE_CENTER
    )


def centreline_runs(route: RouteResult, bend_radius: float) -> list[list[Point3]]:
    """Sample the exact tangent/arc centreline the CAD tube is swept along, one run per segment.

    Runs are kept apart so a bend seen edge-on, which folds back on its own straight in that
    view, is not drawn as one self-overlapping wide polyline.
    """
    runs: list[list[Point3]] = []
    for segment in centreline_segments(route.points, bend_radius):
        if isinstance(segment, ArcSegment):
            runs.append([segment.point_at(step / ARC_STEPS) for step in range(ARC_STEPS + 1)])
        else:
            runs.append([segment.start, segment.end])
    return runs


def _dedupe(points: Iterable[Point2]) -> list[Point2]:
    kept: list[Point2] = []
    for point in points:
        if not kept or abs(point[0] - kept[-1][0]) + abs(point[1] - kept[-1][1]) > 1e-6:
            kept.append(point)
    return kept


def _pipe(msp: Modelspace, view: View, runs: list[list[Point3]], outer_diameter: float) -> None:
    for run in runs:
        projected = _dedupe(view.project(point) for point in run)
        if len(projected) < 2:
            continue  # a straight seen end-on is covered by the bends either side of it
        msp.add_lwpolyline(
            projected,
            dxfattribs={"layer": "PIPE", "const_width": outer_diameter},
        )
        msp.add_lwpolyline(projected, dxfattribs={"layer": "PIPE-CL", "linetype": "CENTER"})


def _nozzles(msp: Modelspace, view: View, config: DemoConfig, text_height: float) -> None:
    radius = config.tube.outer_diameter / 2
    tags: dict[Point2, list[str]] = {}
    for tag, port in (("START", config.start_port), ("END", config.end_port)):
        tags.setdefault(view.project(port.position), []).append(tag)
    for centre, names in tags.items():
        msp.add_circle(centre, radius, dxfattribs={"layer": "NOZZLE"})
        _label(
            msp,
            "/".join(names),
            (centre[0], centre[1] + radius + text_height * 0.8),
            text_height * 0.5,
            "NOZZLE",
        )


def _dimension(msp: Modelspace, start: Point2, end: Point2, offset: Point2, angle: float) -> None:
    base = ((start[0] + end[0]) / 2 + offset[0], (start[1] + end[1]) / 2 + offset[1])
    dimension = msp.add_linear_dim(
        base=base,
        p1=start,
        p2=end,
        angle=angle,
        dimstyle="EZDXF",
        dxfattribs={"layer": "DIMENSION"},
    )
    dimension.render()


def _overall_dimensions(
    msp: Modelspace, view: View, config: DemoConfig, text_height: float
) -> None:
    sizes = config.workspace.dimensions
    horizontal, vertical = view.axes
    x0, y0 = view.origin
    width, height = sizes[horizontal], sizes[vertical]
    clear = text_height * 2.5
    _dimension(msp, (x0, y0), (x0 + width, y0), (0.0, -clear), 0.0)
    _dimension(msp, (x0, y0), (x0, y0 + height), (-clear, 0.0), 90.0)


def _title_block(
    msp: Modelspace,
    config: DemoConfig,
    route: RouteResult,
    report: AssuranceReport,
    corner: Point2,
    text_height: float,
) -> None:
    verdict = "PASS" if report.overall_pass else "FAIL"
    tube = config.tube
    lines = [
        config.title.upper(),
        "SYNTHETIC DATA - NOT FOR CONSTRUCTION",
        "UNITS: MM   MODEL SPACE 1:1   THIRD-ANGLE PROJECTION",
        f"TUBE OD {tube.outer_diameter:g}   BEND RADIUS {tube.minimum_bend_radius:g}"
        f"   CLEARANCE {tube.minimum_clearance:g}",
        (
            f"ROUTE LENGTH {route.length:.1f}   BENDS {len(route.points) - 2}"
            if route.found
            else "NO ROUTE FOUND"
        ),
        f"ASSURANCE (EXACT GEOMETRY CHECKS): {verdict}",
        "GENERATED BY SYNTERA",
    ]
    if not route.found and route.diagnostic:
        lines.insert(5, route.diagnostic.upper()[:90])
    row = text_height * 1.6
    width = text_height * 0.9 * max(len(line) for line in lines) * 0.75
    height = row * (len(lines) + 0.5)
    x0, y0 = corner
    _rectangle(msp, (x0, y0), (x0 + width, y0 + height), "TITLE")
    for index, line in enumerate(lines):
        y = y0 + height - row * (index + 1)
        msp.add_text(
            line,
            height=text_height * (0.9 if index == 0 else 0.6),
            dxfattribs={"layer": "TITLE"},
        ).set_placement((x0 + text_height * 0.5, y), align=TextEntityAlignment.LEFT)


def write_layout_dxf(
    config: DemoConfig, route: RouteResult, report: AssuranceReport, path: Path
) -> None:
    """Write plan, front and right elevations of equipment, nozzles and the routed tube."""
    dimensions = config.workspace.dimensions
    text_height = max(dimensions) / 60
    gap = text_height * 10
    doc = ezdxf.new("R2018", setup=True)
    doc.units = units.MM
    doc.header["$MEASUREMENT"] = 1
    doc.header["$LTSCALE"] = text_height / 5
    for name, colour in LAYERS.items():
        doc.layers.add(name, color=colour)
    style = doc.dimstyles.get("EZDXF")
    style.dxf.dimlfac = 1.0  # ezdxf's default style measures metres in centimetres
    style.dxf.dimtxt = text_height * 0.6
    style.dxf.dimasz = text_height * 0.5
    style.dxf.dimexe = text_height * 0.25
    style.dxf.dimexo = text_height * 0.25
    style.dxf.dimdec = 0
    msp = doc.modelspace()

    runs = centreline_runs(route, config.tube.minimum_bend_radius) if route.found else []
    for view in _views(dimensions, gap):
        horizontal, vertical = view.axes
        lower = view.origin
        upper = (lower[0] + dimensions[horizontal], lower[1] + dimensions[vertical])
        _rectangle(msp, lower, upper, "FRAME")
        _equipment(msp, view, config, text_height)
        _pipe(msp, view, runs, config.tube.outer_diameter)
        _nozzles(msp, view, config, text_height)
        _overall_dimensions(msp, view, config, text_height)
        _label(
            msp,
            view.title,
            ((lower[0] + upper[0]) / 2, upper[1] + text_height * 1.5),
            text_height,
        )

    corner = (dimensions[0] + gap, dimensions[2] + gap)
    _title_block(msp, config, route, report, corner, text_height)
    doc.saveas(path)
