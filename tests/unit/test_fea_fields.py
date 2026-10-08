from __future__ import annotations

import math

from syntera.analysis.fields import SolveOutcome, parse_case_fields, parse_mesh_inp, solve_decks
from syntera.schemas import FeaManifest

MESH = """** generator: test
*NODE, NSET=NALL
1, 0.0, 0.0, 0.0
2, 10.0, 0.0, 0.0
3, 10.0, 10.0, 0.0
4, 0.0, 10.0, 0.0
5, 5.0, 0.0, 0.0
6, 10.0, 5.0, 0.0
7, 5.0, 10.0, 0.0
8, 0.0, 5.0, 0.0
*ELEMENT, TYPE=S8R, ELSET=ETUBE
1, 1, 2, 3, 4, 5, 6, 7, 8
*NSET, NSET=NSTART
1, 4
"""

DAT = """
 displacements (vx,vy,vz) for set NALL and time  0.1000000E+01

         1  0.000000E+00  0.000000E+00  0.000000E+00
         3  3.000000E-03  4.000000E-03  0.000000E+00

 stresses (elem, integ.pnt.,sxx,syy,szz,sxy,sxz,syz) for set ETUBE and time  0.1000000E+01

         1   1  1.0E+02  0.0E+00  0.0E+00  0.0E+00  0.0E+00  0.0E+00 _shell_part
         1   2  2.0E+02  0.0E+00  0.0E+00  0.0E+00  0.0E+00  0.0E+00 _shell_part
"""


def test_mesh_parser_reads_nodes_and_zero_based_connectivity():
    model = parse_mesh_inp(MESH)
    assert len(model.nodes) == 8
    assert model.nodes[2] == [10.0, 10.0, 0.0]
    assert model.elements == [[0, 1, 2, 3, 4, 5, 6, 7]]


def test_case_fields_keep_peak_integration_point_stress_and_vectors():
    model = parse_mesh_inp(MESH)
    fields = parse_case_fields("pressure", DAT, model)
    assert fields.element_von_mises == [200.0]
    assert fields.displacement[2] == [0.003, 0.004, 0.0]
    assert math.hypot(*fields.displacement[2]) == 0.005
    assert fields.displacement[1] == [0.0, 0.0, 0.0]


def test_solver_reports_missing_decks_without_running(tmp_path, monkeypatch):
    manifest = FeaManifest(
        deck_format_version=1,
        generator="test",
        input_sha256="0" * 64,
        generated=False,
        diagnostic="route failed assurance",
        allowable_von_mises_mpa=100,
        maximum_displacement_mm=1,
    )
    monkeypatch.setattr("syntera.analysis.fields.shutil.which", lambda _: "/usr/bin/ccx")
    outcome = solve_decks(tmp_path, manifest)
    assert outcome.solved is False
    assert outcome.diagnostic == "route failed assurance"


def test_solver_reports_absent_calculix(tmp_path, monkeypatch):
    manifest = FeaManifest(
        deck_format_version=1,
        generator="test",
        input_sha256="0" * 64,
        generated=True,
        allowable_von_mises_mpa=100,
        maximum_displacement_mm=1,
    )
    monkeypatch.setattr("syntera.analysis.fields.shutil.which", lambda _: None)
    outcome = solve_decks(tmp_path, manifest)
    assert outcome == SolveOutcome(False, "CalculiX (ccx) is not installed on the server")
