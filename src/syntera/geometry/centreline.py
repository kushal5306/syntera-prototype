"""Exact tube centreline made of tangent straights and circular 90-degree bends."""

from __future__ import annotations

import math
from dataclasses import dataclass

from syntera.schemas import Point3

Vector3 = tuple[float, float, float]


def _sub(left: Point3, right: Point3) -> Vector3:
    return (left[0] - right[0], left[1] - right[1], left[2] - right[2])


def _add_scaled(point: Point3, direction: Vector3, distance: float) -> Point3:
    return (
        point[0] + direction[0] * distance,
        point[1] + direction[1] * distance,
        point[2] + direction[2] * distance,
    )


def dot(left: Vector3, right: Vector3) -> float:
    return left[0] * right[0] + left[1] * right[1] + left[2] * right[2]


def cross(left: Vector3, right: Vector3) -> Vector3:
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def unit_between(start: Point3, end: Point3) -> Vector3:
    length = math.dist(start, end)
    if length <= 1e-9:
        raise ValueError("route contains repeated points")
    delta = _sub(end, start)
    return (delta[0] / length, delta[1] / length, delta[2] / length)


def rotate(vector: Vector3, axis: Vector3, angle: float) -> Vector3:
    """Rodrigues rotation of ``vector`` about unit ``axis`` by ``angle`` radians."""
    cosine, sine = math.cos(angle), math.sin(angle)
    axial = dot(axis, vector) * (1.0 - cosine)
    normal = cross(axis, vector)
    return (
        vector[0] * cosine + normal[0] * sine + axis[0] * axial,
        vector[1] * cosine + normal[1] * sine + axis[1] * axial,
        vector[2] * cosine + normal[2] * sine + axis[2] * axial,
    )


@dataclass(frozen=True)
class LineSegment:
    start: Point3
    end: Point3

    @property
    def length(self) -> float:
        return math.dist(self.start, self.end)

    @property
    def direction(self) -> Vector3:
        return unit_between(self.start, self.end)

    def point_at(self, fraction: float) -> Point3:
        return _add_scaled(self.start, self.direction, self.length * fraction)


@dataclass(frozen=True)
class ArcSegment:
    """Circular arc from ``start`` rotating about unit ``axis`` through ``center``."""

    center: Point3
    start: Point3
    axis: Vector3
    radius: float
    angle: float

    @property
    def length(self) -> float:
        return self.radius * self.angle

    def point_at(self, fraction: float) -> Point3:
        radial = rotate(_sub(self.start, self.center), self.axis, self.angle * fraction)
        return _add_scaled(self.center, radial, 1.0)

    @property
    def end(self) -> Point3:
        return self.point_at(1.0)


Segment = LineSegment | ArcSegment


def centreline_segments(points: list[Point3], bend_radius: float) -> list[Segment]:
    """Replace each orthogonal route corner with a tangent quarter circle."""
    if len(points) < 2:
        raise ValueError("a tube route requires at least two points")
    segments: list[Segment] = []
    current = points[0]
    for index in range(1, len(points) - 1):
        corner = points[index]
        incoming = unit_between(points[index - 1], corner)
        outgoing = unit_between(corner, points[index + 1])
        if abs(dot(incoming, outgoing)) > 1e-9:
            raise ValueError("route contains a non-orthogonal or collinear corner")
        tangent_in = _add_scaled(corner, incoming, -bend_radius)
        tangent_out = _add_scaled(corner, outgoing, bend_radius)
        if math.dist(current, tangent_in) > 1e-7:
            segments.append(LineSegment(current, tangent_in))
        center = _add_scaled(tangent_in, outgoing, bend_radius)
        segments.append(
            ArcSegment(
                center=center,
                start=tangent_in,
                axis=cross(incoming, outgoing),
                radius=bend_radius,
                angle=math.pi / 2.0,
            )
        )
        current = tangent_out
    if math.dist(current, points[-1]) > 1e-7:
        segments.append(LineSegment(current, points[-1]))
    return segments
