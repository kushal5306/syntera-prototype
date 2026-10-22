"""Whole-assembly design audit: is a piped 3D assembly fit for purpose?

Per-route checks verify each pipe against the obstacles it was routed around. This audit
looks at the finished assembly as a reviewer would: every solid against every other solid,
with dimensions measured from the solids themselves. It catches what per-route checks
cannot see by construction: two lines that overlap at a shared nozzle, a nozzle that does
not reach its equipment, a pipe that ends in mid air, or a line that was never routed.

Rules are deterministic and every finding names the parts, the measured value, the
requirement and a location, so an engineer can confirm it in any CAD viewer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import combinations
from typing import Literal

import cadquery as cq
from OCP.Bnd import Bnd_Box
from OCP.BRepBndLib import BRepBndLib
from pydantic import BaseModel, ConfigDict

from syntera.geometry.cad import exact_shape_distance
from syntera.schemas import Point3
from syntera.verification.measure import TUBE_FACE_TYPES, measure_tube

Role = Literal["equipment", "structure", "pipe", "stub"]
VOLUME_TOLERANCE_MM3 = 1e-3
CONTACT_TOLERANCE_MM = 0.5
DIAMETER_TOLERANCE_MM = 0.05
DETOUR_ADVISORY_RATIO = 1.5
DEFAULT_ACCESS_GAP_MM = 800.0


@dataclass(frozen=True)
class PipeEnd:
    """Where a pipe must end and the direction the pipe leaves that point in."""

    position: Point3
    direction: Point3


@dataclass(frozen=True)
class AuditPart:
    name: str
    role: Role
    shape: cq.Shape
    line: str | None = None
    outer_diameter: float | None = None
    minimum_bend_radius: float | None = None
    clearance: float = 0.0
    # points where this part is joined to another part on purpose (port, tee, flange);
    # a nozzle stub lists its flange point on the equipment first
    junctions: tuple[Point3, ...] = ()
    # equipment a nozzle stub is welded to; None for a stub at a battery limit
    owner: str | None = None
    ends: tuple[PipeEnd, ...] = ()
    # shortest axis-aligned distance between the line's terminals, for the detour advisory
    direct_distance: float | None = None
    length: float | None = None


class AuditModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AuditFinding(AuditModel):
    parts: list[str]
    message: str
    measured_mm: float | None = None
    required_mm: float | None = None
    location: Point3 | None = None


class AuditRule(AuditModel):
    id: str
    title: str
    basis: str
    status: Literal["pass", "fail", "advisory"]
    checked: int
    findings: list[AuditFinding]


class AssemblyAudit(AuditModel):
    """Result of auditing every solid of an assembly against every other one."""

    overall_pass: bool
    parts: int
    pairs_checked: int  # pairs close enough to need an exact distance
    rules: list[AuditRule]
    summary: str


@dataclass
class _Rule:
    id: str
    title: str
    basis: str
    advisory: bool = False
    checked: int = 0
    findings: list[AuditFinding] = field(default_factory=list)

    def done(self) -> AuditRule:
        status = "pass" if not self.findings else ("advisory" if self.advisory else "fail")
        return AuditRule(
            id=self.id,
            title=self.title,
            basis=self.basis,
            status=status,  # type: ignore[arg-type]
            checked=self.checked,
            findings=self.findings,
        )


def loose_box(shape: cq.Shape) -> cq.BoundBox:
    """Fast bounding box that may be slightly larger than the shape (never smaller)."""
    box = Bnd_Box()
    BRepBndLib.Add_s(shape.wrapped, box, True)
    return cq.BoundBox(box)


def _round(point: Point3) -> Point3:
    return tuple(round(value, 1) for value in point)  # type: ignore[return-value]


def _shared_junction(a: AuditPart, b: AuditPart) -> Point3 | None:
    for p in a.junctions:
        for q in b.junctions:
            if math.dist(p, q) <= CONTACT_TOLERANCE_MM:
                return p
    return None


def _owned_face(a: AuditPart, b: AuditPart) -> Point3 | None:
    """The flange point of a nozzle stub on its own equipment, if ``a``/``b`` is that pair."""
    for stub, other in ((a, b), (b, a)):
        if stub.role == "stub" and stub.owner == other.name and stub.junctions:
            return stub.junctions[0]
    return None


def _boxes_near(a: cq.BoundBox, b: cq.BoundBox, gap: float) -> bool:
    return not (
        a.xmax + gap < b.xmin
        or b.xmax + gap < a.xmin
        or a.ymax + gap < b.ymin
        or b.ymax + gap < a.ymin
        or a.zmax + gap < b.zmin
        or b.zmax + gap < a.zmin
    )


def _box_centre(box: cq.BoundBox) -> Point3:
    return _round(((box.xmin + box.xmax) / 2, (box.ymin + box.ymax) / 2, (box.zmin + box.zmax) / 2))


def _within(box: cq.BoundBox, centre: Point3, radius: float) -> bool:
    corners = [
        (x, y, z)
        for x in (box.xmin, box.xmax)
        for y in (box.ymin, box.ymax)
        for z in (box.zmin, box.zmax)
    ]
    return all(math.dist(corner, centre) <= radius for corner in corners)


def audit_assembly(
    parts: list[AuditPart],
    *,
    missing_lines: list[str] | None = None,
    access_gap: float = DEFAULT_ACCESS_GAP_MM,
    missing_reason: str = "is not routed",
) -> AssemblyAudit:
    """Audit every part against every other; see the module docstring for the rules."""
    clash = _Rule(
        "clash",
        "No solid intersects another",
        "Exact OpenCascade boolean on every pair of solids. Parts joined on purpose (a pipe "
        "and its nozzle stub, a tee) may meet only within one pipe diameter of the joint.",
    )
    clearance = _Rule(
        "clearance",
        "Pipe clearance to equipment and other pipes",
        "Exact surface-to-surface distance on every unjoined pair that includes a pipe; "
        "required gap is the larger of the two parts' clearance (Syntera default: half the "
        "OD, at least 25 mm).",
    )
    access = _Rule(
        "access",
        "Access way between equipment",
        f"Exact distance between equipment solids of at least {access_gap:g} mm for "
        "operation and maintenance access (Syntera default; set per project).",
    )
    bends = _Rule(
        "bend_radius",
        "Bend radius measured on the solid",
        "Major radius of every toroidal face of each pipe solid, read from the CAD geometry, "
        "against the line's minimum bend radius. A turning pipe with no circular bend faces "
        "is a kink.",
    )
    diameter = _Rule(
        "outside_diameter",
        "Pipe outside diameter matches the line size",
        f"Radius of every cylindrical and toroidal face within {DIAMETER_TOLERANCE_MM} mm "
        "of half the specified OD.",
    )
    connections = _Rule(
        "connections",
        "Every pipe end is connected",
        f"Each pipe has an end cap on each of its ports, facing along the port, within "
        f"{CONTACT_TOLERANCE_MM} mm; each nozzle stub touches its equipment; every pipe end "
        "is joined to a stub or another pipe.",
    )
    complete = _Rule(
        "completeness",
        "Every required line is in the model",
        "Each connection required by the source (P&ID or drawing) has a routed pipe.",
    )
    open_ends = _Rule(
        "open_ends",
        "Open pipe ends (advisory)",
        "In an imported model each pipe end cap should meet a nozzle, equipment or another "
        "pipe; an open end may be a battery limit, vent, drain or a missing connection.",
        advisory=True,
    )
    detour = _Rule(
        "detour",
        "Routing efficiency (advisory)",
        f"Routed length over the shortest axis-aligned distance between the line's ports; "
        f"above {DETOUR_ADVISORY_RATIO:g} the line is a candidate for rerouting or moving "
        "equipment. Advisory only: a detour is never a safety failure.",
        advisory=True,
    )

    # loose boxes are only a broad phase, so the cheap non-optimal ones are enough
    boxes = [loose_box(part.shape) for part in parts]
    largest_gap = max([access_gap, *(part.clearance for part in parts)])
    pairs = 0
    for i, j in combinations(range(len(parts)), 2):
        a, b = parts[i], parts[j]
        both_equipment = a.role == "equipment" and b.role == "equipment"
        required = access_gap if both_equipment else max(a.clearance, b.clearance)
        joint = _shared_junction(a, b) or _owned_face(a, b)
        rule = access if both_equipment else clearance
        clash.checked += 1
        if joint is None:
            rule.checked += 1
        # pairs whose bounding boxes are further apart than any requirement pass outright
        if not _boxes_near(boxes[i], boxes[j], max(required, largest_gap) + 1.0):
            continue
        pairs += 1
        distance = exact_shape_distance(a.shape, b.shape)
        if distance <= CONTACT_TOLERANCE_MM:
            common = a.shape.intersect(b.shape)
            volume = common.Volume()
            if volume > VOLUME_TOLERANCE_MM3:
                box = common.BoundingBox()
                diameter_limit = max(a.outer_diameter or 0.0, b.outer_diameter or 0.0)
                allowed = joint is not None and _within(box, joint, max(diameter_limit, 1.0))
                if not allowed:
                    kind = "joined at a tee or port but overlap beyond it" if joint else "clash"
                    clash.findings.append(
                        AuditFinding(
                            parts=[a.name, b.name],
                            message=f"{a.name} and {b.name} overlap by {volume / 1000:.1f} cm3 "
                            f"({kind}); overlap is {box.xlen:.0f} x {box.ylen:.0f} x "
                            f"{box.zlen:.0f} mm",
                            measured_mm=round(-max(box.xlen, box.ylen, box.zlen), 3),
                            required_mm=0.0,
                            location=_box_centre(box),
                        )
                    )
        if joint is not None:
            continue
        if distance + 1e-6 < required:
            rule.findings.append(
                AuditFinding(
                    parts=[a.name, b.name],
                    message=f"{a.name} is {distance:.1f} mm from {b.name}; "
                    f"{required:.1f} mm required",
                    measured_mm=round(distance, 3),
                    required_mm=round(required, 3),
                    location=_box_centre(boxes[i] if a.role != "equipment" else boxes[j]),
                )
            )

    by_name = {part.name: part for part in parts}
    for part in parts:
        if part.owner is not None:
            connections.checked += 1
            owner = by_name.get(part.owner)
            gap = exact_shape_distance(part.shape, owner.shape) if owner else math.inf
            if gap > CONTACT_TOLERANCE_MM:
                connections.findings.append(
                    AuditFinding(
                        parts=[part.name, part.owner],
                        message=f"{part.name} stops {gap:.1f} mm short of {part.owner}: the "
                        "nozzle is not on the equipment surface",
                        measured_mm=round(gap, 3),
                        required_mm=0.0,
                        location=_round(part.junctions[0]) if part.junctions else None,
                    )
                )
        if part.role != "pipe":
            continue
        measured = measure_tube(part.shape)
        if part.outer_diameter is not None:
            diameter.checked += 1
            wrong = [
                r
                for r in measured.outer_radii
                if abs(r - part.outer_diameter / 2) > DIAMETER_TOLERANCE_MM
            ]
            if wrong:
                diameter.findings.append(
                    AuditFinding(
                        parts=[part.name],
                        message=f"{part.name} has a face of diameter {2 * wrong[0]:.2f} mm; "
                        f"line size is {part.outer_diameter:.2f} mm OD",
                        measured_mm=round(2 * wrong[0], 3),
                        required_mm=part.outer_diameter,
                    )
                )
        if part.minimum_bend_radius is not None:
            bends.checked += 1
            odd = sorted(set(measured.other_face_types) - TUBE_FACE_TYPES)
            smallest = measured.minimum_bend_radius
            if odd:
                bends.findings.append(
                    AuditFinding(
                        parts=[part.name],
                        message=f"{part.name} has {', '.join(odd).lower()} faces; a bent "
                        "tube should only have straight, toroidal and end-cap faces",
                    )
                )
            elif smallest is not None and smallest + 1e-6 < part.minimum_bend_radius:
                bends.findings.append(
                    AuditFinding(
                        parts=[part.name],
                        message=f"{part.name} bends at {smallest:.1f} mm radius; minimum is "
                        f"{part.minimum_bend_radius:.1f} mm",
                        measured_mm=round(smallest, 3),
                        required_mm=part.minimum_bend_radius,
                    )
                )
        for end in part.ends:
            connections.checked += 1
            backwards = tuple(-v for v in end.direction)
            capped = any(
                math.dist(cap.centre, end.position) <= CONTACT_TOLERANCE_MM
                and sum(cap.normal[k] * backwards[k] for k in range(3)) >= 1 - 1e-6
                for cap in measured.ends
            )
            if not capped:
                connections.findings.append(
                    AuditFinding(
                        parts=[part.name],
                        message=f"{part.name} does not end on its port at "
                        f"{_round(end.position)} along the port direction",
                        location=_round(end.position),
                    )
                )
            joined = any(
                other is not part
                and any(math.dist(end.position, q) <= CONTACT_TOLERANCE_MM for q in other.junctions)
                for other in parts
            )
            if not joined:
                connections.findings.append(
                    AuditFinding(
                        parts=[part.name],
                        message=f"{part.name} ends at {_round(end.position)} with nothing "
                        "joined to it (open end)",
                        location=_round(end.position),
                    )
                )
        if not part.ends:
            # No declared ports (an imported model): every end cap should meet another part.
            for cap in measured.ends:
                open_ends.checked += 1
                if not any(
                    other is not part
                    and any(
                        math.dist(cap.centre, q) <= CONTACT_TOLERANCE_MM for q in other.junctions
                    )
                    for other in parts
                ):
                    open_ends.findings.append(
                        AuditFinding(
                            parts=[part.name],
                            message=f"{part.name} has an open end at {_round(cap.centre)}: "
                            "confirm it is a battery limit, vent, drain or blind flange",
                            location=_round(cap.centre),
                        )
                    )
        if part.direct_distance and part.length:
            detour.checked += 1
            ratio = part.length / part.direct_distance
            if ratio > DETOUR_ADVISORY_RATIO:
                detour.findings.append(
                    AuditFinding(
                        parts=[part.name],
                        message=f"{part.name} is {part.length / 1000:.1f} m for ports "
                        f"{part.direct_distance / 1000:.1f} m apart ({ratio:.1f}x)",
                        measured_mm=round(part.length, 3),
                        required_mm=round(part.direct_distance, 3),
                    )
                )

    for line in missing_lines or []:
        complete.findings.append(AuditFinding(parts=[line], message=f"{line} {missing_reason}"))
    complete.checked = len(missing_lines or []) + sum(part.role == "pipe" for part in parts)

    checked = [clash, clearance, connections, bends, diameter, access]
    if missing_lines is not None:
        checked.insert(0, complete)
    advisories = [r for r in (open_ends, detour) if r.checked or r is detour]
    rules = [r.done() for r in (*checked, *advisories)]
    failing = [r for r in rules if r.status == "fail"]
    to_review = sum(len(r.findings) for r in rules if r.status == "advisory")
    if failing:
        summary = f"{sum(len(r.findings) for r in failing)} finding(s) fail: " + ", ".join(
            f"{r.title.lower()} ({len(r.findings)})" for r in failing
        )
    else:
        summary = f"All {len(checked)} rules pass on {len(parts)} solids"
    if to_review:
        summary += f"; {to_review} item(s) to review"
    return AssemblyAudit(
        overall_pass=not failing,
        parts=len(parts),
        pairs_checked=pairs,
        rules=rules,
        summary=summary,
    )
