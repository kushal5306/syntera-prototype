"""Deterministic direction-aware A* search on a 3D occupancy grid."""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass

from syntera.schemas import DemoConfig, RouteResult
from syntera.spatial.occupancy import Index3, OccupancyGrid

Direction = tuple[int, int, int]
State = tuple[Index3, Direction | None, int, int]
NEIGHBOURS: tuple[Direction, ...] = (
    (1, 0, 0),
    (-1, 0, 0),
    (0, 1, 0),
    (0, -1, 0),
    (0, 0, 1),
    (0, 0, -1),
)


def _add(left: Index3, right: Direction) -> Index3:
    return tuple(left[i] + right[i] for i in range(3))  # type: ignore[return-value]


def _direction(vector: tuple[float, float, float]) -> Direction:
    return tuple(int(round(value)) for value in vector)  # type: ignore[return-value]


def _opposite(direction: Direction) -> Direction:
    return tuple(-value for value in direction)  # type: ignore[return-value]


def _heuristic(node: Index3, goal: Index3, resolution: float) -> float:
    return resolution * math.sqrt(sum((node[i] - goal[i]) ** 2 for i in range(3)))


def _compress(points: list[tuple[float, float, float]]) -> list[tuple[float, float, float]]:
    if len(points) < 3:
        return points
    result = [points[0]]
    prior_direction = None
    for index in range(1, len(points)):
        direction = tuple(points[index][axis] - points[index - 1][axis] for axis in range(3))
        if prior_direction is not None and direction != prior_direction:
            result.append(points[index - 1])
        prior_direction = direction
    result.append(points[-1])
    return result


@dataclass(frozen=True)
class AStarRouter:
    grid: OccupancyGrid
    config: DemoConfig

    def route(self) -> RouteResult:
        try:
            start = self.grid.index(self.config.start_port.position)
            goal = self.grid.index(self.config.end_port.position)
        except ValueError as error:
            return RouteResult(found=False, diagnostic=str(error))
        if self.grid.occupied[start]:
            return RouteResult(found=False, diagnostic="start port is inside inflated geometry")
        if self.grid.occupied[goal]:
            return RouteResult(found=False, diagnostic="end port is inside inflated geometry")

        start_direction = _direction(self.config.start_port.direction)
        final_direction = _opposite(_direction(self.config.end_port.direction))
        radius_steps = math.ceil(
            self.config.tube.minimum_bend_radius / self.config.voxel_resolution - 1e-9
        )
        initial: State = (start, None, 0, 0)
        queue: list[tuple[float, float, int, State]] = []
        counter = 0
        heapq.heappush(
            queue, (_heuristic(start, goal, self.grid.resolution), 0.0, counter, initial)
        )
        costs = {initial: 0.0}
        parents: dict[State, State] = {}
        expanded = 0
        final_state: State | None = None

        while queue:
            _, current_cost, _, state = heapq.heappop(queue)
            if current_cost != costs.get(state):
                continue
            node, previous_direction, run_length, bends = state
            expanded += 1
            if (
                node == goal
                and previous_direction == final_direction
                and run_length >= radius_steps
            ):
                final_state = state
                break

            for direction in NEIGHBOURS:
                if previous_direction is None and direction != start_direction:
                    continue
                if previous_direction is not None and direction == _opposite(previous_direction):
                    continue
                turning = previous_direction is not None and direction != previous_direction
                required_run = radius_steps if bends == 0 else 2 * radius_steps
                if turning and run_length < required_run:
                    continue
                neighbour = _add(node, direction)
                if any(
                    value < 0 or value >= self.grid.shape[i] for i, value in enumerate(neighbour)
                ):
                    continue
                if self.grid.occupied[neighbour]:
                    continue
                new_run = 1 if turning else run_length + 1
                new_bends = int(bool(bends or turning))
                next_state: State = (neighbour, direction, new_run, new_bends)
                proximity = self.grid.centreline_clearance(neighbour) - (
                    self.config.tube.outer_diameter / 2.0
                )
                proximity_cost = self.config.routing_cost_weights.obstacle_proximity / max(
                    proximity, self.grid.resolution * 0.1
                )
                step_cost = self.grid.resolution + proximity_cost
                if turning:
                    step_cost += self.config.routing_cost_weights.bend
                candidate = current_cost + step_cost
                if candidate + 1e-12 >= costs.get(next_state, float("inf")):
                    continue
                costs[next_state] = candidate
                parents[next_state] = state
                counter += 1
                priority = candidate + _heuristic(neighbour, goal, self.grid.resolution)
                heapq.heappush(queue, (priority, candidate, counter, next_state))

        if final_state is None:
            return RouteResult(
                found=False,
                diagnostic="no feasible route satisfies occupancy, bend, and port constraints",
                expanded_nodes=expanded,
            )

        states = [final_state]
        while states[-1] != initial:
            states.append(parents[states[-1]])
        states.reverse()
        points = _compress([self.grid.point(state[0]) for state in states])
        polyline_length = sum(
            math.dist(left, right) for left, right in zip(points, points[1:], strict=False)
        )
        bend_count = max(0, len(points) - 2)
        radius = self.config.tube.minimum_bend_radius
        length = polyline_length + bend_count * (math.pi * radius / 2.0 - 2.0 * radius)
        return RouteResult(found=True, points=points, length=length, expanded_nodes=expanded)
