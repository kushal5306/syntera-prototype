"""Measure a swept tube solid from its faces, independently of the route that made it.

Bend radii come from the toroidal faces, the outside radius from the cylindrical and
toroidal faces, and the ends from the planar caps. Nothing here reads route points or the
tube specification, so checks built on these values cannot pass by construction.
"""

from __future__ import annotations

from dataclasses import dataclass

import cadquery as cq
from OCP.BRepAdaptor import BRepAdaptor_Surface

from syntera.schemas import Point3

TUBE_FACE_TYPES = {"CYLINDER", "TORUS", "PLANE"}


@dataclass(frozen=True)
class TubeEnd:
    centre: Point3
    normal: Point3  # outward normal of the end cap


@dataclass(frozen=True)
class TubeMeasurement:
    bend_radii: tuple[float, ...]
    outer_radii: tuple[float, ...]
    ends: tuple[TubeEnd, ...]
    other_face_types: tuple[str, ...]

    @property
    def minimum_bend_radius(self) -> float | None:
        return min(self.bend_radii, default=None)


def measure_tube(shape: cq.Shape) -> TubeMeasurement:
    bends: list[float] = []
    radii: list[float] = []
    ends: list[TubeEnd] = []
    others: list[str] = []
    for face in shape.Faces():
        kind = face.geomType()
        surface = BRepAdaptor_Surface(face.wrapped)
        if kind == "TORUS":
            torus = surface.Torus()
            bends.append(torus.MajorRadius())
            radii.append(torus.MinorRadius())
        elif kind == "CYLINDER":
            radii.append(surface.Cylinder().Radius())
        elif kind == "PLANE":
            centre = face.Center()
            normal = face.normalAt(centre)
            ends.append(TubeEnd(centre.toTuple(), normal.toTuple()))
        else:
            others.append(kind)
    ends.sort(key=lambda end: end.centre)
    return TubeMeasurement(tuple(sorted(bends)), tuple(sorted(radii)), tuple(ends), tuple(others))
