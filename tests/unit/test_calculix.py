from __future__ import annotations

import pytest

from syntera.analysis.calculix import MANIFEST_FILE, input_sha256, write_fea_inputs
from syntera.schemas import DemoConfig, FeaManifest

ROUTE = [(20.0, 100.0, 80.0), (220.0, 100.0, 80.0)]


@pytest.fixture
def config(base_data, analysis_data, copy_data) -> DemoConfig:
    data = copy_data(base_data)
    data["analysis"] = copy_data(analysis_data)
    return DemoConfig.model_validate(data)


def test_decks_are_written_per_load_case_with_provenance(config, tmp_path):
    manifest = write_fea_inputs(config, ROUTE, tmp_path)
    assert manifest.generated is True
    assert manifest.allowable_von_mises_mpa == pytest.approx(250 / 1.5)
    assert {path.name for path in tmp_path.iterdir()} == {
        MANIFEST_FILE,
        "mesh.inp",
        "pressure.inp",
        "weight.inp",
        "thermal.inp",
    }
    saved = FeaManifest.model_validate_json((tmp_path / MANIFEST_FILE).read_text())
    assert saved == manifest
    digest = input_sha256(config, ROUTE)
    for name in ("mesh.inp", "pressure.inp", "weight.inp", "thermal.inp"):
        text = (tmp_path / name).read_text()
        assert f"** input sha256: {digest}" in text
        assert max(len(line) for line in text.splitlines()) <= 132

    pressure = (tmp_path / "pressure.inp").read_text()
    assert "*INCLUDE, INPUT=mesh.inp" in pressure
    assert "*SHELL SECTION, ELSET=ETUBE, MATERIAL=STEEL\n1.5\n" in pressure
    assert "NSTART, 1, 6\nNEND, 1, 6\n" in pressure
    assert "*DLOAD\nETUBE, P, 5\n" in pressure
    assert "NALL, 293.15\n*NODE PRINT" in pressure
    assert "ETUBE, GRAV, 9810, 0, 0, -1" in (tmp_path / "weight.inp").read_text()
    thermal = (tmp_path / "thermal.inp").read_text()
    assert "*EXPANSION, ZERO=293.15\n1.2e-05\n" in thermal
    assert "*TEMPERATURE\nNALL, 333.15\n" in thermal
    assert "*DLOAD" not in thermal

    mesh = (tmp_path / "mesh.inp").read_text()
    assert "*ELEMENT, TYPE=S8R, ELSET=ETUBE" in mesh
    assert "-0.000000" not in mesh


def test_deck_generation_is_byte_for_byte_deterministic(config, tmp_path):
    write_fea_inputs(config, ROUTE, tmp_path / "a")
    write_fea_inputs(config, ROUTE, tmp_path / "b")
    for path in sorted((tmp_path / "a").iterdir()):
        assert path.read_bytes() == (tmp_path / "b" / path.name).read_bytes()


def test_input_hash_tracks_configuration_and_route(config):
    other = config.model_copy(deep=True)
    other.analysis.load_cases[0].internal_pressure_mpa = 6
    assert input_sha256(config, ROUTE) != input_sha256(other, ROUTE)
    shifted = [(20.0, 100.0, 80.0), (210.0, 100.0, 80.0)]
    assert input_sha256(config, ROUTE) != input_sha256(config, shifted)


def test_meshing_failure_writes_no_decks(config, tmp_path):
    config.analysis.mesh.maximum_axial_element_length = 100.0
    config.analysis.mesh.maximum_aspect_ratio = 2.0
    manifest = write_fea_inputs(config, ROUTE, tmp_path)
    assert manifest.generated is False
    assert "aspect ratio" in (manifest.diagnostic or "")
    assert manifest.load_cases == []
    assert {path.name for path in tmp_path.iterdir()} == {MANIFEST_FILE}
