from __future__ import annotations

from copy import deepcopy

import pytest


@pytest.fixture
def base_data() -> dict:
    return {
        "title": "Synthetic test",
        "synthetic_data": True,
        "workspace": {"units": "mm", "dimensions": [240, 200, 160]},
        "obstacles": [],
        "start_port": {"position": [20, 100, 80], "direction": [1, 0, 0]},
        "end_port": {"position": [220, 100, 80], "direction": [-1, 0, 0]},
        "tube": {
            "outer_diameter": 10,
            "minimum_bend_radius": 20,
            "minimum_clearance": 5,
        },
        "voxel_resolution": 10,
        "routing_cost_weights": {
            "bend": 30,
            "obstacle_proximity": 5,
            "port_direction": 1000,
        },
    }


@pytest.fixture
def copy_data():
    return deepcopy
