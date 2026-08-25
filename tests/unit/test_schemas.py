from __future__ import annotations

import pytest
from pydantic import ValidationError

from syntera.schemas import DemoConfig


def test_invalid_port_direction_is_rejected(base_data, copy_data):
    data = copy_data(base_data)
    data["start_port"]["direction"] = [0.707, 0.707, 0]
    with pytest.raises(ValidationError, match="cardinal unit vector"):
        DemoConfig.model_validate(data)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("units", "m"),
        ("dimensions", [240, -1, 160]),
    ],
)
def test_invalid_units_or_dimensions_are_rejected(base_data, copy_data, field, value):
    data = copy_data(base_data)
    data["workspace"][field] = value
    with pytest.raises(ValidationError):
        DemoConfig.model_validate(data)
