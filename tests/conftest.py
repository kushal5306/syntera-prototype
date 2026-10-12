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


def write_plan_layout(path: Path) -> Path:
    """Synthetic plan-only skid layout (mm) with a hand-drawn, longer-than-needed pipe run.

    No elevations are drawn, so heights come from defaults. The run leaves the left nozzle,
    climbs over the top of every item and drops back to the right nozzle, although a
    shorter route passes between the control panel and the pump.
    """
    import ezdxf

    doc = ezdxf.new("R2018")
    doc.units = ezdxf.units.MM
    msp = doc.modelspace()
    for name in ("EQUIP-VESSELS", "PIPING", "NOZZLES", "NOTES"):
        doc.layers.add(name)
    msp.add_circle((1800, 1700), 700, dxfattribs={"layer": "EQUIP-VESSELS"})
    msp.add_text("FEED TANK", dxfattribs={"layer": "NOTES"}).set_placement((1800, 1700))
    for label, (x, y), (w, h) in (
        ("TRANSFER PUMP", (3400, 1500), (900, 700)),
        ("BAG FILTER", (4700, 1900), (600, 600)),
        ("CONTROL PANEL", (2800, 500), (600, 400)),
    ):
        corners = [
            (x - w / 2, y - h / 2),
            (x + w / 2, y - h / 2),
            (x + w / 2, y + h / 2),
            (x - w / 2, y + h / 2),
        ]
        msp.add_lwpolyline(corners, close=True, dxfattribs={"layer": "EQUIP-VESSELS"})
        msp.add_text(label, dxfattribs={"layer": "NOTES"}).set_placement((x, y))
    route = [(300, 600), (1000, 600), (1000, 2700), (5000, 2700), (5000, 600), (5700, 600)]
    msp.add_lwpolyline(route, dxfattribs={"layer": "PIPING"})
    for point in (route[0], route[-1]):
        msp.add_circle(point, 30, dxfattribs={"layer": "NOZZLES"})
    doc.saveas(path)
    return path


@pytest.fixture
def plan_layout_dxf(tmp_path: Path) -> Path:
    return write_plan_layout(tmp_path / "plan-layout.dxf")
