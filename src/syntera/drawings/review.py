"""Traceable review output for a drawing check: the errors and the reasoning behind each route.

Everything here restates measurements the checks already made, in the drawing's own
coordinates, so a reviewer can find each point on the drawing and confirm it in any CAD
viewer. Nothing here changes a verdict.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import cadquery as cq

from syntera.drawings.layout3d import DrawnLine
from syntera.drawings.models import (
    Evidence,
    Issue,
    Limit,
    LineCheck,
    Location,
    PlantCheck,
    RouteCost,
    RouteRationale,
)
from syntera.geometry.cad import box_gap, exact_shape_distance, obstacle_shape
from syntera.schemas import DemoConfig, Point3
from syntera.verification.assembly_audit import (
    CONTACT_TOLERANCE_MM,
    VOLUME_TOLERANCE_MM3,
    AssemblyAudit,
    AuditPart,
)

OTHER_LINE = "drawn line "  # obstacle names for other lines' legs in a line's routing
LIMITS_SHOWN = 3


def drawing_point(point: Sequence[float], origin: tuple[float, float]) -> Location:
    return Location(
        x=round(point[0] + origin[0], 1), y=round(point[1] + origin[1], 1), el=round(point[2], 1)
    )


def _part_label(name: str) -> str:
    """'drawn line 80-P-2003-A1A leg 2' reads better as '80-P-2003-A1A (leg 2)'."""
    if name.startswith(OTHER_LINE):
        line, _, leg = name[len(OTHER_LINE) :].rpartition(" leg ")
        return f"{line} (leg {leg})"
    return name


def _cost(length: float | None, bends: int, bend_weight: float) -> RouteCost:
    penalty = round(bends * bend_weight, 3)
    return RouteCost(
        length_mm=length,
        bends=bends,
        bend_penalty_mm=penalty,
        score_mm=None if length is None else round(length + penalty, 3),
    )


def plant_check(
    tube: cq.Shape,
    line: str,
    partners: set[str],
    parts: list[AuditPart],
    clearance: float,
) -> PlantCheck:
    """The proposed tube against every other part of the drawn plant (not its own line,
    nor a line it is teed into, which it must touch)."""
    clashes: list[str] = []
    nearest: tuple[float, str] | None = None
    for part in parts:
        if part.line == line or (part.role == "pipe" and part.name in partners):
            continue
        if box_gap(tube, part.shape) > max(clearance, part.clearance) + 1.0:
            continue
        distance = exact_shape_distance(tube, part.shape)
        if distance <= CONTACT_TOLERANCE_MM and tube.intersect(part.shape).Volume() > (
            VOLUME_TOLERANCE_MM3
        ):
            clashes.append(part.name)
        if nearest is None or distance < nearest[0]:
            nearest = (distance, part.name)
    required = max([clearance, *(p.clearance for p in parts if nearest and p.name == nearest[1])])
    passes = not clashes and (nearest is None or nearest[0] + 1e-6 >= required)
    return PlantCheck(
        passes=passes,
        clashes=clashes,
        minimum_clearance_mm=None if nearest is None else round(nearest[0], 1),
        closest_part=None if nearest is None else nearest[1],
    )


def route_rationale(
    line: DrawnLine,
    check: LineCheck,
    config: DemoConfig,
    tube: cq.Shape | None,
    plant: PlantCheck | None,
) -> RouteRationale:
    """Why Syntera proposes the route it does for one line, and what it was checked against."""
    bend_weight = config.routing_cost_weights.bend
    drawn, proposed = check.drawn, check.optimised
    resolution = config.voxel_resolution
    moves = {
        "start": round(math.dist(line.points[0], config.start_port.position), 1),
        "end": round(math.dist(line.points[-1], config.end_port.position), 1),
    }
    limiting: list[Limit] = []
    if tube is not None:
        distances = sorted(
            (exact_shape_distance(tube, obstacle_shape(o)), o.name) for o in config.obstacles
        )
        limiting = [
            Limit(
                name=_part_label(name),
                distance_mm=round(distance, 1),
                required_mm=config.tube.minimum_clearance,
            )
            for distance, name in distances[:LIMITS_SHOWN]
        ]
    objective = (
        f"Shortest path on a {resolution:g} mm grid that keeps the pipe's "
        f"{config.tube.minimum_clearance:g} mm clearance from equipment, columns and the "
        f"other drawn lines, with each bend counted as {bend_weight:g} mm of extra pipe and a "
        "small penalty for running close to obstacles. Straights must fit the "
        f"{config.tube.minimum_bend_radius:g} mm bend radius. The result is then checked on "
        "the exact CAD solid."
    )
    if not proposed.buildable:
        why = "; ".join(proposed.failure_reasons) or "no route found"
        summary = f"Syntera found no route it could verify ({why}), so it proposes nothing."
    else:
        saving = (drawn.length_mm or 0.0) - (proposed.length_mm or 0.0)
        bends = drawn.bends - proposed.bends
        change = (
            f"{saving / 1000:.1f} m shorter"
            if saving > 0.5
            else f"{-saving / 1000:.1f} m longer"
            if saving < -0.5
            else "the same length"
        )
        bend_text = (
            f"{bends} fewer bend(s)"
            if bends > 0
            else f"{-bends} more bend(s)"
            if bends < 0
            else "the same bends"
        )
        closest = (
            f" Closest: {limiting[0].name} at {limiting[0].distance_mm:.0f} mm "
            f"({limiting[0].required_mm:g} mm required)."
            if limiting
            else ""
        )
        moved = max(moves.values())
        snap = (
            f" Its ends sit up to {moved:.0f} mm from the drawn nozzles because of the "
            f"{resolution:g} mm grid; the last legs must be adjusted to the real nozzles."
            if moved > 0.5
            else ""
        )
        if plant is None:
            plant_text = ""
        elif plant.passes:
            plant_text = " Checked against the rest of the drawn plant: no clash."
        elif plant.clashes:
            plant_text = (
                " Against the rest of the drawn plant it clashes with "
                + ", ".join(plant.clashes)
                + ", so it is not a drop-in replacement."
            )
        else:
            plant_text = (
                f" Against the rest of the drawn plant it comes {plant.minimum_clearance_mm:.0f} "
                f"mm from {plant.closest_part}, inside the required clearance."
            )
        summary = (
            f"Syntera's route is {change} with {bend_text}.{closest}{snap}{plant_text}"
            if drawn.length_mm is not None
            else f"Syntera's route passes every check the drawn run fails.{closest}{snap}"
            f"{plant_text}"
        )
    return RouteRationale(
        summary=summary,
        objective=objective,
        weights={
            "bend_mm": bend_weight,
            "proximity_mm": config.routing_cost_weights.obstacle_proximity,
        },
        drawn=_cost(drawn.length_mm, drawn.bends, bend_weight),
        proposed=_cost(proposed.length_mm, proposed.bends, bend_weight),
        limiting=limiting,
        nozzle_moves_mm=moves,
        plant_check=plant,
    )


def _short_leg(points: list[Point3], radius: float) -> tuple[int, float, float] | None:
    """First leg too short for the bends at its ends: (leg number, length, needed)."""
    last = len(points) - 2
    for index, (a, b) in enumerate(zip(points, points[1:], strict=False)):
        needed = radius * ((index > 0) + (index < last))
        if math.dist(a, b) + 1e-6 < needed:
            return index + 1, math.dist(a, b), needed
    return None


def _fix_for(rule: str, roles: Sequence[str], parts: Sequence[str], gap: float | None) -> str:
    need = f" by at least {gap:.0f} mm" if gap else ""
    if rule in {"clash", "clearance"}:
        if list(roles).count("pipe") == 2:
            return (
                f"Run one of {parts[0]} and {parts[1]} at another elevation, or offset it in "
                f"plan{need}, where they cross."
            )
        pipe = next((p for p, r in zip(parts, roles, strict=False) if r == "pipe"), parts[0])
        other = next((p for p in parts if p != pipe), parts[-1])
        return f"Reroute {pipe} clear of {other}{need}."
    if rule == "access":
        return f"Move {parts[0]} and {parts[1]} apart{need} for operation and maintenance."
    if rule == "connections":
        return "Connect the end to a nozzle, tee or battery limit, or mark it as a vent or drain."
    return "Correct the drawing so the line meets the rule, then check it again."


def plant_issues(
    audit: AssemblyAudit,
    lines: list[DrawnLine],
    checks: list[LineCheck],
    parts: list[AuditPart],
    origin: tuple[float, float],
) -> list[Issue]:
    """One issue per real error in the drawing, merging a clash with its 0 mm clearance."""
    roles = {part.name: part.role for part in parts}
    line_of = {part.name: part.line for part in parts}
    issues: list[Issue] = []
    seen: set[tuple[str, ...]] = set()
    by_rule = {rule.id: rule for rule in audit.rules if rule.status == "fail"}
    required = {
        tuple(sorted(f.parts)): f.required_mm
        for f in getattr(by_rule.get("clearance"), "findings", [])
    }
    for rule_id in (
        "clash",
        "clearance",
        "access",
        "connections",
        "bend_radius",
        "outside_diameter",
    ):
        rule = by_rule.get(rule_id)
        if rule is None:
            continue
        for finding in rule.findings:
            key = tuple(sorted(finding.parts))
            if key in seen:
                continue
            seen.add(key)
            gap = required.get(key) if rule_id == "clash" else finding.required_mm
            part_roles = [roles.get(name, "equipment") for name in finding.parts]
            evidence = Evidence(
                measured_mm=finding.measured_mm,
                required_mm=gap if rule_id == "clash" else finding.required_mm,
                text=finding.message,
            )
            names = [line_of.get(name) for name in finding.parts]
            issues.append(
                Issue(
                    id=f"{rule_id}-{len(issues) + 1}",
                    rule=rule_id,
                    title=(
                        f"{' and '.join(finding.parts)} clash"
                        if rule_id == "clash"
                        else f"{rule.title}: {', '.join(finding.parts)}"
                    ),
                    lines=sorted({name for name in names if name}),
                    parts=list(finding.parts),
                    what=finding.message,
                    where=drawing_point(finding.location, origin) if finding.location else None,
                    evidence=evidence,
                    fix=_fix_for(rule_id, part_roles, finding.parts, gap),
                )
            )
    for line, check in zip(lines, checks, strict=True):
        if check.drawn.buildable:
            continue
        spec = line.tube
        short = _short_leg(line.points, spec.minimum_bend_radius) if spec else None
        if short and not line.problems:
            number, length, needed = short
            a, b = line.points[number - 1], line.points[number]
            where = drawing_point([(a[i] + b[i]) / 2 for i in range(3)], origin)
            fix = (
                f"Lengthen leg {number} to at least {needed:.0f} mm (move the level change or "
                "the corner), or use short-radius elbows if the pipe class allows them."
            )
            evidence = Evidence(measured_mm=round(length, 1), required_mm=round(needed, 1),
                                text=check.drawn.failure_reasons[0])  # fmt: skip
        else:
            where = drawing_point(line.points[0], origin)
            fix = "Correct the run so each end meets a nozzle, tee or battery limit."
            evidence = Evidence(text="; ".join(check.drawn.failure_reasons))
        issues.append(
            Issue(
                id=f"buildable-{len(issues) + 1}",
                rule="buildable",
                title=f"{line.name} cannot be built as drawn",
                lines=[line.name],
                parts=[line.name],
                what=f"{line.name}: " + "; ".join(check.drawn.failure_reasons),
                where=where,
                evidence=evidence,
                fix=fix,
            )
        )
    return issues
