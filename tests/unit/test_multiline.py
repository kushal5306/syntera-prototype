"""Multi-line routing helpers: order, obstacles, nozzle reserves and pair checks (B1)."""

from __future__ import annotations

import json
from pathlib import Path

import cadquery as cq
import pytest

from syntera.design import DesignError, parse_design
from syntera.multiline import (
    ORDER_RULE,
    check_nozzles_unshared,
    line_config,
    nozzle_reserve,
    order_rationale,
    pair_check,
    pipe_obstacles,
    routing_order,
)
from syntera.schemas import TubeSpecification

PROJECT = Path(__file__).parents[2]
THREE_LINES = PROJECT / "examples" / "demo_three_lines.design.json"


def _design(**changes):
    raw = json.loads(THREE_LINES.read_text(encoding="utf-8"))
    raw.update(changes)
    return parse_design(raw)


def _bounds(box):
    return [(box.center[i] - box.size[i] / 2, box.center[i] + box.size[i] / 2) for i in range(3)]


def test_routing_order_is_largest_bore_first_then_line_id():
    design = _design()
    assert [line.id for line in routing_order(design)] == ["header", "branch", "drain"]
    raw = json.loads(THREE_LINES.read_text(encoding="utf-8"))
    for line in raw["lines"]:
        line["tube"] = {"minimum_bend_radius": 30, "minimum_clearance": 20, "outer_diameter": 20}
    assert [line.id for line in routing_order(parse_design(raw))] == ["branch", "drain", "header"]


def test_nozzles_shared_by_two_lines_are_refused():
    raw = json.loads(THREE_LINES.read_text(encoding="utf-8"))
    raw["lines"][1]["from_nozzle"] = "branch-in"
    with pytest.raises(DesignError, match="branch-in: branch, drain"):
        check_nozzles_unshared(parse_design(raw))
    check_nozzles_unshared(_design())


def test_pipe_obstacles_contain_every_leg_and_bend():
    tube = TubeSpecification(outer_diameter=20, minimum_bend_radius=30, minimum_clearance=10)
    points = [(0.0, 100.0, 100.0), (200.0, 100.0, 100.0), (200.0, 300.0, 100.0)]
    boxes = pipe_obstacles("a", points, tube)
    assert [box.name for box in boxes] == ["line a leg 1", "line a leg 2", "line a bend 1"]
    assert _bounds(boxes[0]) == [(-10, 210), (90, 110), (90, 110)]
    assert _bounds(boxes[2]) == [(160, 240), (60, 140), (60, 140)]
    wider = pipe_obstacles("a", points, tube, margin=5)
    assert _bounds(wider[0]) == [(-15, 215), (85, 115), (85, 115)]


def test_nozzle_reserve_runs_along_the_port_and_stays_in_the_workspace():
    design = _design()
    tube = design.line("header").tube
    reserve = nozzle_reserve("header", design.nozzle("header-in"), tube, (800, 500, 400))
    # 40 mm from the wall, along +x for bend radius 45 + OD 30; half-width 15 + 20
    assert _bounds(reserve) == [(40, 115), (65, 135), (65, 135)]
    edge = design.nozzle("drain-in").model_copy(
        update={"port": design.nozzle("drain-in").port.model_copy(update={"position": (5, 40, 5)})}
    )
    clipped = nozzle_reserve("drain", edge, design.line("drain").tube, (800, 500, 400))
    low = [bound[0] for bound in _bounds(clipped)]
    assert min(low) >= 0


def test_line_config_adds_earlier_pipes_and_pending_nozzles():
    design = _design()
    header, branch, drain = routing_order(design)
    points = [(40.0, 100.0, 100.0), (760.0, 100.0, 100.0)]
    config = line_config(design, branch, [(header, points)], [drain])
    names = [obstacle.name for obstacle in config.obstacles]
    assert names[:2] == ["process-vessel", "transfer-pump"]
    assert "line header leg 1" in names
    assert "line drain nozzle drain-in reserve" in names
    assert "line drain nozzle drain-out reserve" in names
    assert not any("branch" in name for name in names)
    assert config.start_port == design.nozzle("branch-in").port
    assert config.tube == branch.tube


def test_order_rationale_is_a_traceable_rule_record():
    design = _design()
    record = order_rationale(design.line("drain"), 3, 3)
    assert record.source == "rule" and record.rule_id == ORDER_RULE
    assert record.value == 3
    assert "3 of 3" in record.explanation.format(value=record.value, unit="", **record.inputs)


def _rod(y: float) -> cq.Shape:
    return cq.Solid.makeCylinder(10, 200, cq.Vector(0, y, 0), cq.Vector(1, 0, 0))


def test_pair_check_measures_exact_clearance_and_intersection():
    design = _design()
    header, branch = design.line("header"), design.line("branch")
    apart = pair_check(header, branch, _rod(0), _rod(100))
    assert apart.passed and not apart.intersects
    assert apart.lines == ("branch", "header")
    assert apart.minimum_distance_mm == pytest.approx(80)
    assert apart.required_clearance_mm == 20

    close = pair_check(header, branch, _rod(0), _rod(35))
    assert not close.passed and not close.intersects
    assert close.minimum_distance_mm == pytest.approx(15)

    crossing = pair_check(header, branch, _rod(0), _rod(5))
    assert crossing.intersects and not crossing.passed
