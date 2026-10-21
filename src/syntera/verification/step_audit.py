"""Audit any STEP assembly: classify its solids, find the joints, then run the design audit.

Nothing about how the model was made is needed. A solid is taken to be pipe when it is a
single swept tube: only cylindrical, toroidal and two planar end faces, one outside
diameter, no larger than ``max_pipe_od``. Every other solid is equipment or structure. Two
parts are joined where an end cap of a pipe lies on or inside the other part; those joints
are the only places parts may meet.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import cadquery as cq
from OCP.BRepClass3d import BRepClass3d_SolidClassifier
from OCP.gp import gp_Pnt
from OCP.TopAbs import TopAbs_IN, TopAbs_ON

from syntera.geometry.step_import import StepImportError, _read_solids
from syntera.schemas import Point3
from syntera.verification.assembly_audit import (
    CONTACT_TOLERANCE_MM,
    DEFAULT_ACCESS_GAP_MM,
    AssemblyAudit,
    AuditPart,
    audit_assembly,
    loose_box,
)
from syntera.verification.measure import TubeMeasurement, measure_tube

DEFAULT_MAX_PIPE_OD_MM = 610.0  # DN600; larger round solids are taken to be vessels
DEFAULT_BEND_FACTOR = 1.5  # long-radius elbow, R = 1.5 x OD
MAX_SOLIDS = 2000


@dataclass(frozen=True)
class StepAuditResult:
    audit: AssemblyAudit
    parts: list[AuditPart]
    pipes: int
    equipment: int
    sizes_mm: tuple[float, ...]  # distinct pipe outside diameters found


def _pipe_od(measured: TubeMeasurement, max_pipe_od: float) -> float | None:
    if measured.other_face_types or len(measured.ends) != 2 or not measured.outer_radii:
        return None
    low, high = measured.outer_radii[0], measured.outer_radii[-1]
    if high - low > 0.05 or 2 * high > max_pipe_od:
        return None
    return round(2 * high, 3)


def _touches(point: Point3, shape: cq.Shape, box: cq.BoundBox) -> bool:
    """True when ``point`` is inside or within contact tolerance of ``shape``."""
    if not (
        box.xmin - CONTACT_TOLERANCE_MM <= point[0] <= box.xmax + CONTACT_TOLERANCE_MM
        and box.ymin - CONTACT_TOLERANCE_MM <= point[1] <= box.ymax + CONTACT_TOLERANCE_MM
        and box.zmin - CONTACT_TOLERANCE_MM <= point[2] <= box.zmax + CONTACT_TOLERANCE_MM
    ):
        return False
    for solid in shape.Solids():
        state = BRepClass3d_SolidClassifier(solid.wrapped, gp_Pnt(*point), CONTACT_TOLERANCE_MM)
        if state.State() in (TopAbs_IN, TopAbs_ON):
            return True
    return False


def audit_step(
    path: Path,
    *,
    max_pipe_od: float = DEFAULT_MAX_PIPE_OD_MM,
    bend_factor: float = DEFAULT_BEND_FACTOR,
    access_gap: float = DEFAULT_ACCESS_GAP_MM,
) -> StepAuditResult:
    """Read every solid of a STEP file and audit the assembly they form."""
    solids = _read_solids(path)
    if len(solids) > MAX_SOLIDS:
        raise StepImportError(
            f"{path.name} has {len(solids)} solids; the audit handles up to {MAX_SOLIDS}"
        )
    parts: list[AuditPart] = []
    caps: dict[int, list[Point3]] = {}
    for index, solid in enumerate(solids, start=1):
        measured = measure_tube(solid)
        od = _pipe_od(measured, max_pipe_od)
        if od is None:
            parts.append(
                AuditPart(name=f"solid {index} (equipment)", role="equipment", shape=solid)
            )
            continue
        caps[len(parts)] = [end.centre for end in measured.ends]
        parts.append(
            AuditPart(
                name=f"solid {index} (pipe OD {od:g})",
                role="pipe",
                shape=solid,
                outer_diameter=od,
                minimum_bend_radius=bend_factor * od,
                clearance=max(25.0, od / 2),
            )
        )

    boxes = [loose_box(part.shape) for part in parts]
    junctions: dict[int, list[Point3]] = {i: list(points) for i, points in caps.items()}
    owners: dict[int, str] = {}
    for i, points in caps.items():
        for point in points:
            for j, other in enumerate(parts):
                if j == i or not _touches(point, other.shape, boxes[j]):
                    continue
                junctions.setdefault(j, []).append(point)
                if other.role == "equipment" and i not in owners:
                    owners[i] = other.name
                    # the flange point leads the list so the audit treats it as the nozzle
                    junctions[i].remove(point)
                    junctions[i].insert(0, point)
    parts = [
        replace(part, junctions=tuple(junctions.get(i, ())), owner=owners.get(i))
        for i, part in enumerate(parts)
    ]
    audit = audit_assembly(parts, access_gap=access_gap)
    sizes = tuple(sorted({p.outer_diameter for p in parts if p.outer_diameter is not None}))
    pipes = sum(p.role == "pipe" for p in parts)
    return StepAuditResult(audit, parts, pipes, len(parts) - pipes, sizes)
