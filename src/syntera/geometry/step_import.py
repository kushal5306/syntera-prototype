"""STEP import and conservative defeaturing of real equipment geometry.

The imported solid is kept unchanged for exact verification. A separate routing solid is
derived by removing small curved features (holes, rounds) only when each removal is proven
to add material, so occupancy built from it can only be more conservative than the original.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import cadquery as cq
from OCP.BRepAlgoAPI import BRepAlgoAPI_Defeaturing
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
from OCP.BRepClass3d import BRepClass3d_SolidClassifier
from OCP.BRepExtrema import BRepExtrema_DistShapeShape
from OCP.gp import gp_Pnt
from OCP.IFSelect import IFSelect_RetDone
from OCP.STEPControl import STEPControl_Reader
from OCP.TopAbs import TopAbs_IN, TopAbs_ON

from syntera.schemas import Point3, StepImportSummary, StepObstacle

ROUND_GEOMETRY = {"CYLINDER", "SPHERE", "TORUS"}
CONTAINMENT_TOLERANCE_MM3 = 1e-6


class StepImportError(ValueError):
    """Raised when STEP geometry cannot be imported or trusted."""


@dataclass(frozen=True)
class ImportedStep:
    """Exact imported geometry plus the conservative solid used for routing occupancy."""

    original: cq.Shape
    routing: cq.Shape
    routing_solids: tuple[cq.Solid, ...]
    summary: StepImportSummary


def _read_solids(path: Path) -> list[cq.Solid]:
    reader = STEPControl_Reader()
    if reader.ReadFile(str(path)) != IFSelect_RetDone:
        raise StepImportError(f"STEP file {path} could not be read")
    if reader.TransferRoots() == 0:
        raise StepImportError(f"STEP file {path} contains no transferable geometry")
    solids = cq.Shape.cast(reader.OneShape()).Solids()
    if not solids:
        raise StepImportError(f"STEP file {path} contains no closed solids")
    for index, solid in enumerate(solids):
        if not solid.isValid() or solid.Volume() <= 0:
            raise StepImportError(f"STEP file {path} solid {index} is not a valid closed solid")
    return solids


def _face_radius(face: cq.Face) -> float | None:
    surface = face._geomAdaptor()
    geometry = face.geomType()
    if geometry == "CYLINDER":
        return surface.Cylinder().Radius()
    if geometry == "SPHERE":
        return surface.Sphere().Radius()
    if geometry == "TORUS":
        return surface.Torus().MinorRadius()
    return None


def _feature_key(faces: list[cq.Face]) -> tuple:
    """Deterministic, position-based identity for a feature that survives re-meshing."""
    return tuple(
        sorted(
            (*(round(value, 6) for value in face.Center().toTuple()), round(face.Area(), 6))
            for face in faces
        )
    )


def _candidate_features(solid: cq.Solid, max_radius: float) -> list[list[cq.Face]]:
    """Group small round faces that share edges into features, in deterministic order."""
    candidates = [
        face
        for face in solid.Faces()
        if face.geomType() in ROUND_GEOMETRY
        and (radius := _face_radius(face)) is not None
        and radius < max_radius
    ]
    groups: list[list[cq.Face]] = []
    unvisited = list(range(len(candidates)))
    edges = [face.Edges() for face in candidates]
    while unvisited:
        stack = [unvisited.pop(0)]
        group: list[int] = []
        while stack:
            current = stack.pop()
            group.append(current)
            for other in list(unvisited):
                if any(a.isSame(b) for a in edges[current] for b in edges[other]):
                    unvisited.remove(other)
                    stack.append(other)
        groups.append([candidates[index] for index in sorted(group)])
    return sorted(groups, key=_feature_key)


def _remove_feature(solid: cq.Solid, faces: list[cq.Face]) -> cq.Solid | None:
    """Remove faces and accept the result only if it is a valid superset of ``solid``."""
    algorithm = BRepAlgoAPI_Defeaturing()
    algorithm.SetShape(solid.wrapped)
    for face in faces:
        algorithm.AddFaceToRemove(face.wrapped)
    algorithm.SetRunParallel(False)
    algorithm.Build()
    if not algorithm.IsDone():
        return None
    solids = cq.Shape.cast(algorithm.Shape()).Solids()
    if len(solids) != 1 or not solids[0].isValid():
        return None
    result = solids[0]
    if len(result.Faces()) >= len(solid.Faces()):
        return None  # the algorithm reported success without removing anything
    tolerance = CONTAINMENT_TOLERANCE_MM3 * max(1.0, solid.Volume())
    if result.Volume() + tolerance < solid.Volume():
        return None
    if solid.cut(result).Volume() > tolerance:
        return None
    return result


def defeature_solid(solid: cq.Solid, max_radius: float) -> tuple[cq.Solid, int, int, int]:
    """Return (routing solid, candidate count, removed count, rejected count).

    Features are attempted smallest-key first; each success reduces the face count, so the
    loop terminates after at most one attempt per face plus one per rejected feature.
    """
    if max_radius <= 0:
        return solid, 0, 0, 0
    current = solid
    candidates = len(_candidate_features(solid, max_radius))
    rejected: set[tuple] = set()
    removed = 0
    while True:
        features = [
            faces
            for faces in _candidate_features(current, max_radius)
            if _feature_key(faces) not in rejected
        ]
        if not features:
            break
        result = _remove_feature(current, features[0])
        if result is None:
            rejected.add(_feature_key(features[0]))
        else:
            current = result
            removed += 1
    return current, candidates, removed, len(rejected)


def _bounding_box_solid(shape: cq.Shape) -> cq.Solid:
    box = shape.BoundingBox()
    return cq.Solid.makeBox(box.xlen, box.ylen, box.zlen, cq.Vector(box.xmin, box.ymin, box.zmin))


@lru_cache(maxsize=16)
def _load(
    name: str,
    path: str,
    modified_ns: int,
    translation: Point3,
    max_feature_radius: float,
    envelope: str,
) -> ImportedStep:
    del modified_ns  # part of the cache key only, so edited files are re-imported
    source = Path(path)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    offset = cq.Vector(*translation)
    solids = [solid.translate(offset) for solid in _read_solids(source)]
    original = cq.Compound.makeCompound(solids)

    candidates = removed = rejected = 0
    if envelope == "bounding_box":
        routing_solids = [_bounding_box_solid(original)]
    else:
        routing_solids = []
        for solid in solids:
            routed, solid_candidates, solid_removed, solid_rejected = defeature_solid(
                solid, max_feature_radius
            )
            routing_solids.append(routed)
            candidates += solid_candidates
            removed += solid_removed
            rejected += solid_rejected
    routing = cq.Compound.makeCompound(routing_solids)
    box = routing.BoundingBox()
    summary = StepImportSummary(
        name=name,
        source_path=str(source),
        source_sha256=digest,
        solid_count=len(solids),
        envelope=envelope,  # type: ignore[arg-type]
        candidate_features=candidates,
        removed_features=removed,
        rejected_features=rejected,
        original_volume_mm3=sum(solid.Volume() for solid in solids),
        routing_volume_mm3=sum(solid.Volume() for solid in routing_solids),
        routing_bounding_box_mm=(
            (box.xmin, box.ymin, box.zmin),
            (box.xmax, box.ymax, box.zmax),
        ),
    )
    return ImportedStep(original, routing, tuple(routing_solids), summary)


def import_step_obstacle(obstacle: StepObstacle) -> ImportedStep:
    """Import, place, and defeature a STEP obstacle (cached per file revision)."""
    source = Path(obstacle.path).resolve()
    if not source.is_file():
        raise StepImportError(f"STEP obstacle {obstacle.name!r}: file {source} does not exist")
    return _load(
        obstacle.name,
        str(source),
        source.stat().st_mtime_ns,
        tuple(obstacle.translation),  # type: ignore[arg-type]
        obstacle.defeaturing.max_feature_radius,
        obstacle.defeaturing.envelope,
    )


def point_to_imported_distance(point: Point3, imported: ImportedStep) -> float:
    """Exact distance from a point to the routing solid (zero inside or on it)."""
    target = gp_Pnt(*point)
    for solid in imported.routing_solids:
        box = solid.BoundingBox()
        if (
            box.xmin <= point[0] <= box.xmax
            and box.ymin <= point[1] <= box.ymax
            and (box.zmin <= point[2] <= box.zmax)
        ):
            classifier = BRepClass3d_SolidClassifier(solid.wrapped, target, 1e-7)
            if classifier.State() in (TopAbs_IN, TopAbs_ON):
                return 0.0
    vertex = BRepBuilderAPI_MakeVertex(target).Vertex()
    calculation = BRepExtrema_DistShapeShape(vertex, imported.routing.wrapped)
    calculation.Perform()
    if not calculation.IsDone():
        raise StepImportError(f"distance to imported obstacle {imported.summary.name!r} failed")
    return float(calculation.Value())
