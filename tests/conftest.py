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


@pytest.fixture
def analysis_data() -> dict:
    return {
        "wall_thickness": 1.5,
        "material": {
            "name": "steel",
            "elastic_modulus_mpa": 200000,
            "poisson_ratio": 0.3,
            "yield_strength_mpa": 250,
            "density_tonne_per_mm3": 7.85e-9,
            "thermal_expansion_per_k": 1.2e-5,
        },
        "load_cases": [
            {"name": "pressure", "internal_pressure_mpa": 5},
            {"name": "weight", "gravity_mm_per_s2": [0, 0, -9810]},
            {"name": "thermal", "temperature_change_k": 40},
        ],
        "acceptance": {"stress_safety_factor": 1.5, "maximum_displacement_mm": 1.0},
    }
