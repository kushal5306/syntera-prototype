from __future__ import annotations

import math

import pytest

from syntera.analysis.mesh import MeshQualityError, mesh_tube
from syntera.schemas import MeshControls

BENT_ROUTE = [(0.0, 0.0, 0.0), (100.0, 0.0, 0.0), (100.0, 100.0, 0.0), (100.0, 100.0, 100.0)]


def _normal(nodes, element):
    a, b, d = (nodes[element[index] - 1] for index in (0, 1, 3))
    u = [b[i] - a[i] for i in range(3)]
    v = [d[i] - a[i] for i in range(3)]
    return (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])


def test_straight_tube_mesh_is_structured_on_the_mid_surface():
    mesh = mesh_tube([(0.0, 0.0, 0.0), (100.0, 0.0, 0.0)], 20.0, 10.0, 1.0, MeshControls())
    assert mesh.mid_surface_radius == pytest.approx(4.5)
    circumference_length = 2 * math.pi * 4.5 / 16
    assert mesh.axial_elements == math.ceil(100.0 / circumference_length)
    assert len(mesh.elements) == mesh.axial_elements * 16
    corner_rings = mesh.axial_elements + 1
    assert len(mesh.nodes) == corner_rings * 32 + mesh.axial_elements * 16
    assert all(math.hypot(y, z) == pytest.approx(4.5) for _, y, z in mesh.nodes)
    assert len(mesh.start_ring) == len(mesh.end_ring) == 32
    assert all(mesh.nodes[node - 1][0] == pytest.approx(0.0) for node in mesh.start_ring)
    assert all(mesh.nodes[node - 1][0] == pytest.approx(100.0) for node in mesh.end_ring)
    assert all(len(set(element)) == 8 for element in mesh.elements)


def test_element_normals_point_away_from_the_centreline():
    mesh = mesh_tube([(0.0, 0.0, 0.0), (50.0, 0.0, 0.0)], 20.0, 10.0, 1.0, MeshControls())
    for element in mesh.elements:
        normal = _normal(mesh.nodes, element)
        first = mesh.nodes[element[0] - 1]
        assert normal[1] * first[1] + normal[2] * first[2] > 0


def test_bent_tube_mesh_is_deterministic_and_reaches_both_ports():
    controls = MeshControls(minimum_elements_per_bend=8)
    first = mesh_tube(BENT_ROUTE, 30.0, 12.0, 1.0, controls)
    second = mesh_tube(BENT_ROUTE, 30.0, 12.0, 1.0, controls)
    assert first == second
    assert len({node for element in first.elements for node in element}) == len(first.nodes)
    for ring, port in ((first.start_ring, BENT_ROUTE[0]), (first.end_ring, BENT_ROUTE[-1])):
        for node in ring:
            assert math.dist(first.nodes[node - 1], port) == pytest.approx(5.5)
    assert first.maximum_aspect_ratio < controls.maximum_aspect_ratio


def test_coarse_axial_mesh_fails_aspect_ratio_limit():
    controls = MeshControls(maximum_axial_element_length=60.0, maximum_aspect_ratio=3.0)
    with pytest.raises(MeshQualityError, match="aspect ratio"):
        mesh_tube([(0.0, 0.0, 0.0), (120.0, 0.0, 0.0)], 20.0, 10.0, 1.0, controls)
