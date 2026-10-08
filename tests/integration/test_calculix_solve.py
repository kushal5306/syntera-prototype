"""Solve generated decks with CalculiX when ``ccx`` is installed (skipped otherwise)."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from syntera.analysis.calculix import input_sha256, write_fea_inputs
from syntera.analysis.results import evaluate_acceptance, parse_dat
from syntera.schemas import DemoConfig

pytestmark = pytest.mark.skipif(shutil.which("ccx") is None, reason="CalculiX ccx not installed")

ROUTE = [(0.0, 100.0, 100.0), (400.0, 100.0, 100.0)]


def test_straight_tube_matches_closed_form_results(base_data, copy_data, tmp_path):
    data = copy_data(base_data)
    data["workspace"]["dimensions"] = [400, 200, 200]
    data["start_port"]["position"] = list(ROUTE[0])
    data["end_port"]["position"] = list(ROUTE[1])
    data["tube"]["outer_diameter"] = 22
    data["analysis"] = {
        "wall_thickness": 2,
        "material": {
            "name": "steel",
            "elastic_modulus_mpa": 200000,
            "poisson_ratio": 0.3,
            "yield_strength_mpa": 250,
            "thermal_expansion_per_k": 1.2e-5,
        },
        "load_cases": [
            {"name": "pressure", "internal_pressure_mpa": 10},
            {"name": "thermal", "temperature_change_k": 50},
        ],
        "acceptance": {"stress_safety_factor": 1.6, "maximum_displacement_mm": 0.1},
    }
    config = DemoConfig.model_validate(data)
    manifest = write_fea_inputs(config, ROUTE, tmp_path)
    for case in manifest.load_cases:
        subprocess.run(
            ["ccx", "-i", case.name], cwd=tmp_path, check=True, capture_output=True, timeout=300
        )

    # Thin-wall radial growth at mid-span: r * (sigma_hoop - nu * sigma_axial) / E,
    # with sigma_hoop = p * r_inner / t and axially clamped ends.
    pressure = parse_dat((tmp_path / "pressure.dat").read_text())
    assert pressure.maximum_displacement_mm == pytest.approx(0.0022, rel=0.1)
    assert pressure.maximum_von_mises_mpa == pytest.approx(50.0, rel=0.1)
    # Fully restrained thermal expansion: |sigma_axial| = E * alpha * dT = 120 MPa.
    thermal = parse_dat((tmp_path / "thermal.dat").read_text())
    assert thermal.maximum_von_mises_mpa >= 120.0 * 0.98

    report = evaluate_acceptance(manifest, tmp_path, input_sha256(config, ROUTE))
    assert report.overall_pass is False
    assert [case.passed for case in report.load_cases] == [True, False]
