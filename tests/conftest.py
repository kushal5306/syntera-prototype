from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import cadquery as cq
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
def filleted_plate_step(tmp_path: Path) -> Path:
    """Synthetic 120 x 80 x 40 mm plate centred on the origin, generated at test time.

    It has four convex corner rounds (r6), one through hole (r5), and a pocket whose
    four concave corner rounds (r3) cannot be removed without removing material.
    """
    plate = (
        cq.Workplane()
        .box(120, 80, 40)
        .edges("|Z")
        .fillet(6)
        .faces(">Z")
        .workplane()
        .hole(10)
        .faces(">Z")
        .workplane()
        .rect(60, 40)
        .cutBlind(-15)
        .edges("|Z and (not <X) and (not >X) and (not <Y) and (not >Y)")
        .fillet(3)
    )
    path = tmp_path / "plate.step"
    cq.exporters.export(plate, str(path))
    return path


@pytest.fixture
def vessel_step(tmp_path: Path) -> Path:
    """Synthetic 100 x 160 x 180 mm vessel block centred on the origin with a nozzle bore."""
    vessel = (
        cq.Workplane().box(100, 160, 180).edges("|Z").fillet(8).faces(">Z").workplane().hole(12, 30)
    )
    path = tmp_path / "vessel.step"
    cq.exporters.export(vessel, str(path))
    return path
