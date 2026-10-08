"""Structured quadratic-shell (S8R) mesh of the routed tube mid-surface."""

from __future__ import annotations

import math
from dataclasses import dataclass

from syntera.geometry.centreline import (
    ArcSegment,
    LineSegment,
    Segment,
    Vector3,
    centreline_segments,
    cross,
    dot,
    rotate,
)
from syntera.schemas import MeshControls, Point3

_CARDINAL_AXES: tuple[Vector3, ...] = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


class MeshQualityError(ValueError):
    """Raised when the generated mesh violates a deterministic quality limit."""


@dataclass(frozen=True)
class ShellMesh:
    """Nodes are numbered from 1 in list order; elements use S8R node ordering."""

    nodes: list[Point3]
    elements: list[tuple[int, ...]]
    start_ring: list[int]
    end_ring: list[int]
    mid_surface_radius: float
    axial_elements: int
    circumferential_elements: int
    maximum_aspect_ratio: float


@dataclass(frozen=True)
class _Station:
    point: Point3
    e1: Vector3
    e2: Vector3


def _normalise(vector: Vector3) -> Vector3:
    length = math.sqrt(dot(vector, vector))
    return (vector[0] / length, vector[1] / length, vector[2] / length)


def _initial_e1(tangent: Vector3) -> Vector3:
    """Pick the first cardinal axis least aligned with the tangent (stable tie-break)."""
    reference = min(_CARDINAL_AXES, key=lambda axis: abs(dot(axis, tangent)))
    projection = dot(reference, tangent)
    return _normalise(
        (
            reference[0] - projection * tangent[0],
            reference[1] - projection * tangent[1],
            reference[2] - projection * tangent[2],
        )
    )


def _division_count(segment: Segment, controls: MeshControls, target_length: float) -> int:
    by_length = max(1, math.ceil(segment.length / target_length - 1e-9))
    if isinstance(segment, ArcSegment):
        by_angle = math.ceil(controls.minimum_elements_per_bend * segment.angle / (math.pi / 2))
        return max(by_length, by_angle)
    return by_length


def _stations(segments: list[Segment], divisions: list[int]) -> list[_Station]:
    """Sample the centreline at corner and mid-side stations with a parallel-transport frame."""
    first = segments[0]
    tangent = first.direction if isinstance(first, LineSegment) else _arc_tangent(first, 0.0)
    e1 = _initial_e1(tangent)
    stations: list[_Station] = []
    for index, (segment, count) in enumerate(zip(segments, divisions, strict=True)):
        start_e1 = e1
        for step in range(0 if index == 0 else 1, 2 * count + 1):
            fraction = step / (2 * count)
            if isinstance(segment, ArcSegment):
                tangent = _arc_tangent(segment, fraction)
                e1 = rotate(start_e1, segment.axis, segment.angle * fraction)
            else:
                tangent = segment.direction
            stations.append(_Station(segment.point_at(fraction), e1, cross(tangent, e1)))
    return stations


def _arc_tangent(segment: ArcSegment, fraction: float) -> Vector3:
    radial = (
        segment.start[0] - segment.center[0],
        segment.start[1] - segment.center[1],
        segment.start[2] - segment.center[2],
    )
    return _normalise(cross(segment.axis, rotate(radial, segment.axis, segment.angle * fraction)))


def _aspect_ratio(nodes: list[Point3], element: tuple[int, ...]) -> float:
    corners = [nodes[node - 1] for node in element[:4]]
    edges = [math.dist(corners[index], corners[(index + 1) % 4]) for index in range(4)]
    return max(edges) / min(edges)


def mesh_tube(
    points: list[Point3],
    bend_radius: float,
    outer_diameter: float,
    wall_thickness: float,
    controls: MeshControls,
) -> ShellMesh:
    """Mesh the tube mid-surface with a deterministic structured S8R grid.

    Element normals point away from the centreline; CalculiX applies a positive
    shell pressure along the normal, so it acts outwards as internal pressure.
    """
    radius = (outer_diameter - wall_thickness) / 2.0
    circumferential = controls.circumferential_elements
    target_length = (
        controls.maximum_axial_element_length or 2.0 * math.pi * radius / circumferential
    )
    segments = centreline_segments(points, bend_radius)
    divisions = [_division_count(segment, controls, target_length) for segment in segments]
    stations = _stations(segments, divisions)
    ring_size = 2 * circumferential

    node_ids: dict[tuple[int, int], int] = {}
    nodes: list[Point3] = []
    for i, station in enumerate(stations):
        for j in range(ring_size):
            if i % 2 and j % 2:
                continue
            angle = math.pi * j / circumferential
            cosine, sine = math.cos(angle), math.sin(angle)
            nodes.append(
                tuple(  # type: ignore[arg-type]
                    station.point[axis]
                    + radius * (cosine * station.e1[axis] + sine * station.e2[axis])
                    for axis in range(3)
                )
            )
            node_ids[(i, j)] = len(nodes)

    def node(i: int, j: int) -> int:
        return node_ids[(i, j % ring_size)]

    axial = (len(stations) - 1) // 2
    elements: list[tuple[int, ...]] = []
    for a in range(axial):
        for b in range(circumferential):
            i, j = 2 * a, 2 * b
            elements.append(
                (
                    node(i, j),
                    node(i, j + 2),
                    node(i + 2, j + 2),
                    node(i + 2, j),
                    node(i, j + 1),
                    node(i + 1, j + 2),
                    node(i + 2, j + 1),
                    node(i + 1, j),
                )
            )

    worst = max(_aspect_ratio(nodes, element) for element in elements)
    if worst > controls.maximum_aspect_ratio:
        raise MeshQualityError(
            f"element aspect ratio {worst:.3f} exceeds limit {controls.maximum_aspect_ratio:.3f}"
        )
    last = len(stations) - 1
    return ShellMesh(
        nodes=nodes,
        elements=elements,
        start_ring=[node(0, j) for j in range(ring_size)],
        end_ring=[node(last, j) for j in range(ring_size)],
        mid_surface_radius=radius,
        axial_elements=axial,
        circumferential_elements=circumferential,
        maximum_aspect_ratio=worst,
    )
