"""Rationale for the values Syntera chooses when it turns a P&ID into a proposed layout.

Pure functions over the P&ID document and the deterministic layout; they explain the
sizing, placement and pipe rules in ``syntera.pid.layout`` and change none of them.
"""

from __future__ import annotations

from syntera.pid.layout import (
    ACCESS_GAP_MM,
    DEFAULT_DN,
    SKID_MARGIN_MM,
    Layout,
    clearance_for,
    envelope,
)
from syntera.pid.models import PidDocument
from syntera.rationale import Rationale, RationaleSource, weights_rationale

OD_RULE = "pid.outside_diameter_from_nominal_size.v1"
BEND_RULE = "pid.bend_radius_long_radius.v1"
CLEARANCE_RULE = "pid.clearance_half_od_min_25.v1"
WEIGHTS_RULE = "pid.routing_weights_from_grid.v1"
PLACEMENT_RULE = "pid.grid_placement_swaps.v1"
WORKSPACE_RULE = "pid.skid_workspace.v1"
OD_REFERENCE = "ISO 4200 / ASME B36.10 outside diameters (nearest nominal size)"


def line_rationale(nominal_diameter: int | None, outer: float, step: float) -> list[Rationale]:
    """Records for one line's tube and routing values, matching ``build_plant``."""
    stated = nominal_diameter is not None
    od = Rationale(
        path="tube.outer_diameter",
        value=outer,
        unit="mm",
        source="rule" if stated else "default",
        rule_id=OD_RULE,
        inputs={"nominal_diameter": nominal_diameter if stated else DEFAULT_DN},
        explanation=(
            "outside diameter {value} {unit} for the line's nominal size DN{nominal_diameter}."
            if stated
            else "the P&ID gives no nominal size, so DN{nominal_diameter} ({value} {unit}) "
            "is assumed."
        ),
        references=(OD_REFERENCE,),
    )
    bend = Rationale(
        path="tube.minimum_bend_radius",
        value=1.5 * outer,
        unit="mm",
        source="rule",
        rule_id=BEND_RULE,
        inputs={"outer_diameter_mm": outer},
        explanation="bend radius {value} {unit} is 1.5 x the {outer_diameter_mm:g} mm OD "
        "(long-radius elbow).",
    )
    clearance = Rationale(
        path="tube.minimum_clearance",
        value=clearance_for(outer),
        unit="mm",
        source="rule",
        rule_id=CLEARANCE_RULE,
        inputs={"outer_diameter_mm": outer},
        explanation="clearance {value} {unit} is half the {outer_diameter_mm:g} mm OD, "
        "and never less than 25 mm.",
    )
    weights = weights_rationale(4 * step, step, 1000.0, step, WEIGHTS_RULE)
    return [od, bend, clearance, *weights]


def plant_rationale(document: PidDocument, layout: Layout, step: float) -> list[Rationale]:
    """Records for equipment sizes and positions and the skid workspace."""
    records: list[Rationale] = []
    by_tag = {item.tag: item for item in document.equipment}
    keys: set[str] = set()
    for placed in sorted(layout.equipment, key=lambda e: e.tag):
        env = envelope(by_tag[placed.tag])
        key = _key(placed.tag)
        while key in keys:
            key += "_"
        keys.add(key)
        source: RationaleSource
        if env.source == "default":
            source = "default"
        elif env.rule_id is not None:
            source = "calculation"
        else:
            source = "drawing"
        records.append(
            Rationale(
                path=f"equipment.{key}.size",
                value=tuple(float(v) for v in placed.size),
                unit="mm",
                source=source,
                rule_id=env.rule_id,
                inputs={"component_class": placed.component_class, "note": env.note},
                explanation="{component_class} envelope {value} {unit}: {note}.",
            )
        )
        records.append(
            Rationale(
                path=f"equipment.{key}.center",
                value=tuple(float(v) for v in placed.center),
                unit="mm",
                source="calculation",
                rule_id=PLACEMENT_RULE,
                inputs={"gap_mm": ACCESS_GAP_MM},
                explanation="placed at {value} {unit} on a grid of equal cells with "
                "{gap_mm:g} mm access gaps, then swapped between cells while that shortened "
                "connected nozzles' straight-line distance. A P&ID gives no positions.",
            )
        )
    records.append(
        Rationale(
            path="plant.workspace",
            value=tuple(float(v) for v in layout.workspace),
            unit="mm",
            source="calculation",
            rule_id=WORKSPACE_RULE,
            inputs={"margin_mm": SKID_MARGIN_MM, "step_mm": step},
            explanation="skid envelope {value} {unit}: the used grid cells plus a "
            "{margin_mm:g} mm margin, tall enough for the tallest item and its headroom, "
            "rounded up to the {step_mm:g} mm routing grid.",
        )
    )
    return records


def _key(tag: str) -> str:
    """Tag as a rationale path segment (letters, digits, '_' and '-' only)."""
    return "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in tag) or "_"
