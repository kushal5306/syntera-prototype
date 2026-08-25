from __future__ import annotations

from syntera.routing.astar import AStarRouter
from syntera.schemas import DemoConfig
from syntera.spatial.occupancy import OccupancyGrid


def route(data: dict):
    config = DemoConfig.model_validate(data)
    return AStarRouter(OccupancyGrid.from_config(config), config).route()


def test_clear_route_has_no_bends(base_data):
    result = route(base_data)
    assert result.found
    assert result.points == [(20.0, 100.0, 80.0), (220.0, 100.0, 80.0)]


def test_route_around_one_obstacle(base_data, copy_data):
    data = copy_data(base_data)
    data["obstacles"] = [
        {
            "type": "box",
            "name": "block",
            "center": [120, 100, 80],
            "size": [40, 80, 80],
        }
    ]
    result = route(data)
    assert result.found
    assert len(result.points) >= 4


def test_completely_blocked_workspace(base_data, copy_data):
    data = copy_data(base_data)
    data["obstacles"] = [
        {
            "type": "box",
            "name": "wall",
            "center": [120, 100, 80],
            "size": [20, 200, 160],
        }
    ]
    result = route(data)
    assert not result.found
    assert "no feasible route" in result.diagnostic


def test_insufficient_clearance_fails_closed(base_data, copy_data):
    data = copy_data(base_data)
    data["tube"]["minimum_clearance"] = 25
    result = route(data)
    assert not result.found
    assert "port is inside inflated geometry" in result.diagnostic


def test_repeated_execution_is_deterministic(base_data, copy_data):
    data = copy_data(base_data)
    data["obstacles"] = [
        {
            "type": "box",
            "name": "block",
            "center": [120, 100, 80],
            "size": [40, 80, 80],
        }
    ]
    assert route(data).model_dump(exclude={"expanded_nodes"}) == route(data).model_dump(
        exclude={"expanded_nodes"}
    )
