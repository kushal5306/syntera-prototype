from __future__ import annotations

import pytest

from syntera.analysis.results import evaluate_acceptance, parse_dat, von_mises
from syntera.schemas import FeaLoadCaseDeck, FeaManifest

DAT = """
                        S T E P       1

 displacements (vx,vy,vz) for set NALL and time  0.1000000E+01

         1  3.000000E-01  0.000000E+00  4.000000E-01
         2 -1.000000E-02  0.000000E+00  0.000000E+00

 stresses (elem, integ.pnt.,sxx,syy,szz,sxy,sxz,syz) for set ETUBE and time  0.1000000E+01

         1   1  1.0E+02  0.0E+00  0.0E+00  0.0E+00  0.0E+00  0.0E+00 _shell_0000000001
         1   2  0.0E+00  0.0E+00  0.0E+00  1.0E+01  0.0E+00  0.0E+00 _shell_0000000001
"""


def _manifest(**overrides) -> FeaManifest:
    values = {
        "deck_format_version": 1,
        "generator": "test",
        "input_sha256": "abc",
        "generated": True,
        "allowable_von_mises_mpa": 120.0,
        "maximum_displacement_mm": 1.0,
        "load_cases": [FeaLoadCaseDeck(name="case", deck_file="case.inp", result_file="case.dat")],
    }
    values.update(overrides)
    return FeaManifest(**values)


def test_von_mises_matches_closed_form_cases():
    assert von_mises(100, 0, 0, 0, 0, 0) == pytest.approx(100)
    assert von_mises(0, 0, 0, 10, 0, 0) == pytest.approx(10 * 3**0.5)
    assert von_mises(50, 50, 50, 0, 0, 0) == pytest.approx(0)


def test_dat_parser_reads_peak_values_and_ignores_part_labels():
    results = parse_dat(DAT)
    assert results.maximum_displacement_mm == pytest.approx(0.5)
    assert results.maximum_von_mises_mpa == pytest.approx(100)


def test_acceptance_passes_within_thresholds(tmp_path):
    (tmp_path / "case.dat").write_text(DAT)
    report = evaluate_acceptance(_manifest(), tmp_path, "abc")
    assert report.overall_pass is True
    assert report.load_cases[0].maximum_von_mises_mpa == pytest.approx(100)


def test_acceptance_fails_on_exceeded_limits(tmp_path):
    (tmp_path / "case.dat").write_text(DAT)
    manifest = _manifest(allowable_von_mises_mpa=90.0, maximum_displacement_mm=0.4)
    report = evaluate_acceptance(manifest, tmp_path, "abc")
    assert report.overall_pass is False
    assert any("von Mises" in reason for reason in report.failure_reasons)
    assert any("displacement" in reason for reason in report.failure_reasons)


@pytest.mark.parametrize(
    ("contents", "digest", "message"),
    [
        (None, "abc", "missing"),
        ("", "abc", "no finite stress"),
        (DAT, "other", "does not match"),
    ],
)
def test_acceptance_fails_closed(tmp_path, contents, digest, message):
    if contents is not None:
        (tmp_path / "case.dat").write_text(contents)
    report = evaluate_acceptance(_manifest(), tmp_path, digest)
    assert report.overall_pass is False
    assert any(message in reason for reason in report.failure_reasons)


def test_ungenerated_manifest_fails(tmp_path):
    manifest = _manifest(generated=False, diagnostic="meshing failed safely", load_cases=[])
    report = evaluate_acceptance(manifest, tmp_path, "abc")
    assert report.failure_reasons == ["meshing failed safely"]
