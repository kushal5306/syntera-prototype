from __future__ import annotations

import math

import ezdxf
import pytest

from syntera.reporting.layout_dxf import centreline_runs, write_layout_dxf
from syntera.schemas import DemoConfig, RouteResult
from syntera.verification.checks import failed_report

ROUTE = [(20.0, 100.0, 80.0), (80.0, 100.0, 80.0), (80.0, 160.0, 80.0), (220.0, 160.0, 80.0)]


@pytest.fixture
def config(base_data) -> DemoConfig:
    base_data["end_port"] = {"position": [220, 160, 80], "direction": [-1, 0, 0]}
    base_data["obstacles"] = [
        {"type": "box", "name": "vessel", "center": [150, 100, 60], "size": [40, 40, 80]},
        {
            "type": "cylinder",
            "name": "pump",
            "center": [40, 40, 40],
            "radius": 15,
            "height": 50,
            "axis": "z",
        },
        {
            "type": "cylinder",
            "name": "drum",
            "center": [180, 40, 40],
            "radius": 15,
            "height": 50,
            "axis": "x",
        },
    ]
    return DemoConfig.model_validate(base_data)


def write(config: DemoConfig, route: RouteResult, tmp_path):
    path = tmp_path / "layout.dxf"
    write_layout_dxf(config, route, failed_report(config, route, 0.0), path)
    return ezdxf.readfile(path)


def texts(doc) -> list[str]:
    return [entity.dxf.text for entity in doc.modelspace().query("TEXT")]


def test_centreline_runs_follow_ports_and_bend_radius():
    runs = centreline_runs(RouteResult(found=True, points=ROUTE, length=0.0), 20.0)
    assert runs[0][0] == ROUTE[0]
    assert runs[-1][-1] == pytest.approx(ROUTE[-1])
    for start, end in zip(runs, runs[1:], strict=False):
        assert start[-1] == pytest.approx(end[0])
    # First bend turns +X into +Y about the corner (80, 100): its centre is (60, 120).
    bend = runs[1]
    assert all(math.dist(point, (60.0, 120.0, 80.0)) == pytest.approx(20.0) for point in bend)


def test_layout_has_three_views_with_overall_dimensions(config, tmp_path):
    doc = write(config, RouteResult(found=True, points=ROUTE, length=240.0), tmp_path)
    msp = doc.modelspace()
    assert doc.units == ezdxf.units.MM
    assert {"FRAME", "EQUIPMENT", "PIPE", "PIPE-CL", "NOZZLE", "DIMENSION", "TITLE"} <= {
        layer.dxf.name for layer in doc.layers
    }
    measurements = sorted(round(dim.get_measurement(), 6) for dim in msp.query("DIMENSION"))
    assert measurements == [160, 160, 200, 200, 240, 240]
    labels = texts(doc)
    assert {"PLAN", "FRONT ELEVATION (LOOKING +Y)", "RIGHT ELEVATION (LOOKING -X)"} <= set(labels)
    assert labels.count("VESSEL") == labels.count("PUMP") == labels.count("DRUM") == 3
    assert "ASSURANCE (EXACT GEOMETRY CHECKS): FAIL" in labels
    assert "ROUTE LENGTH 240.0   BENDS 2" in labels
    # The z-axis pump is a circle only in plan; the x-axis drum only in the right elevation.
    assert len(msp.query("CIRCLE[layer=='EQUIPMENT']")) == 2
    pipes = msp.query("LWPOLYLINE[layer=='PIPE']")
    assert pipes and all(pipe.dxf.const_width == 10 for pipe in pipes)


def test_layout_without_route_draws_no_pipe(config, tmp_path):
    route = RouteResult(found=False, diagnostic="A* exhausted the search space")
    doc = write(config, route, tmp_path)
    assert not doc.modelspace().query("LWPOLYLINE[layer=='PIPE']")
    labels = texts(doc)
    assert "NO ROUTE FOUND" in labels
    assert "A* EXHAUSTED THE SEARCH SPACE" in labels
    assert labels.count("START") == labels.count("END") == 3


def test_ports_seen_end_on_share_one_label(base_data, tmp_path):
    # Both default ports sit at y=100, z=80, so the right elevation sees them coincide.
    doc = write(DemoConfig.model_validate(base_data), RouteResult(found=False), tmp_path)
    labels = texts(doc)
    assert labels.count("START") == labels.count("END") == 2
    assert labels.count("START/END") == 1
