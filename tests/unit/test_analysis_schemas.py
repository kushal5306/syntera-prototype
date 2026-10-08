from __future__ import annotations

import pytest
from pydantic import ValidationError

from syntera.schemas import DemoConfig


def _config(base_data, analysis_data, copy_data) -> dict:
    data = copy_data(base_data)
    data["analysis"] = copy_data(analysis_data)
    return data


def test_valid_analysis_section_is_accepted(base_data, analysis_data, copy_data):
    config = DemoConfig.model_validate(_config(base_data, analysis_data, copy_data))
    assert config.analysis is not None
    assert [case.name for case in config.analysis.load_cases] == ["pressure", "weight", "thermal"]
    assert config.analysis.mesh.circumferential_elements == 16


def test_analysis_is_optional(base_data):
    assert DemoConfig.model_validate(base_data).analysis is None


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda a: a["load_cases"].append({"name": "idle"}), "applies no load"),
        (
            lambda a: a["load_cases"].append({"name": "pressure", "internal_pressure_mpa": 1}),
            "unique",
        ),
        (lambda a: a["material"].pop("density_tonne_per_mm3"), "density"),
        (lambda a: a["material"].pop("thermal_expansion_per_k"), "thermal expansion"),
        (lambda a: a["load_cases"][0].update(name="Bad Name"), "pattern"),
        (lambda a: a["material"].update(poisson_ratio=0.5), "poisson_ratio"),
        (lambda a: a["acceptance"].update(stress_safety_factor=0.9), "stress_safety_factor"),
        (lambda a: a.update(load_cases=[]), "load_cases"),
    ],
)
def test_invalid_analysis_inputs_are_rejected(base_data, analysis_data, copy_data, mutate, message):
    data = _config(base_data, analysis_data, copy_data)
    mutate(data["analysis"])
    with pytest.raises(ValidationError, match=message):
        DemoConfig.model_validate(data)


def test_wall_thickness_must_leave_a_bore(base_data, analysis_data, copy_data):
    data = _config(base_data, analysis_data, copy_data)
    data["analysis"]["wall_thickness"] = 5
    with pytest.raises(ValidationError, match="wall thickness"):
        DemoConfig.model_validate(data)
