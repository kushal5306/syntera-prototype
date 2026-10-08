from __future__ import annotations

import math

import cadquery as cq
import pytest

from syntera.reporting.metrics import package_metrics, piping_metrics
from syntera.schemas import DemoConfig

DEMO_ROUTE = [
    (40.0, 200.0, 100.0),
    (140.0, 200.0, 100.0),
    (140.0, 320.0, 100.0),
    (480.0, 320.0, 100.0),
    (480.0, 200.0, 100.0),
    (560.0, 200.0, 100.0),
]


def test_piping_splits_route_into_straights_and_exact_bends():
    piping = piping_metrics(DEMO_ROUTE, 40)
    # 760 mm of polyline; each 90 degree bend trades 2R of straight for a quarter circle.
    assert piping.bends == 4
    assert piping.straights == 5
    assert piping.straight_length_mm == pytest.approx(760 - 4 * 80, abs=1e-3)
    assert piping.bend_length_mm == pytest.approx(4 * math.pi * 40 / 2, abs=1e-3)
    assert piping.main_length_mm == pytest.approx(440 + 80 * math.pi, abs=1e-3)


def test_package_envelope_is_union_of_exact_part_bounds(base_data):
    config = DemoConfig.model_validate(base_data)
    equipment = cq.Solid.makeBox(100, 200, 300)
    nozzle = cq.Solid.makeBox(10, 10, 10, pnt=cq.Vector(150, 0, 0))
    port = cq.Solid.makeBox(1, 1, 1)
    metrics = package_metrics(config, [equipment, nozzle, port], None, [])
    assert metrics.envelope_min_mm == pytest.approx((0, 0, 0), abs=1e-3)
    assert metrics.envelope_max_mm == pytest.approx((160, 200, 300), abs=1e-3)
    assert metrics.envelope_volume_m3 == pytest.approx(160 * 200 * 300 / 1e9, rel=1e-4)
    assert metrics.footprint_m2 == pytest.approx(160 * 200 / 1e6, rel=1e-4)
    assert metrics.height_mm == pytest.approx(300, abs=1e-3)
    assert metrics.frame_volume_m3 == pytest.approx(240 * 200 * 160 / 1e9)
    assert metrics.piping is None
    assert "routed-tube" not in metrics.included_parts


def test_tube_extends_the_envelope_and_reports_piping(base_data):
    config = DemoConfig.model_validate(base_data)
    equipment = cq.Solid.makeBox(50, 50, 50)
    tube = cq.Solid.makeBox(200, 10, 10, pnt=cq.Vector(0, 100, 0))
    metrics = package_metrics(config, [equipment], tube, [(0.0, 105.0, 5.0), (200.0, 105.0, 5.0)])
    assert metrics.envelope_dimensions_mm == pytest.approx((200, 110, 50), abs=1e-3)
    assert metrics.included_parts[-1] == "routed-tube"
    assert metrics.piping is not None
    assert metrics.piping.main_length_mm == pytest.approx(200)
    assert metrics.piping.bends == 0
