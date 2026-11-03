"""Deterministic explanation engine: why a value is what it is (roadmap E3).

``explain_value`` builds a structured explanation of one design value from its rationale
record, the rule that set it, the knowledge entries for that rule and their citations.
``explain_entity`` and ``back_calculate_velocity`` explain what a foreign drawing shows;
everything they say is labelled as inference, because a drawing states geometry, not
reasons. Two modes: ``plain`` for anyone, ``engineer`` with inputs, formulae and rule ids.

Explanations describe how values were chosen. They never state or change whether a
design passes; that comes only from the exact geometry checks.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from syntera.design import DesignDocument
from syntera.knowledge.retrieval import build_index
from syntera.knowledge.store import KnowledgeBase, KnowledgeEntry, load_knowledge
from syntera.rationale import Rationale, RationaleValue
from syntera.rules.sizing import VELOCITY_RULE, RuleError, flow_velocity
from syntera.rules.tables import SYNTHETIC_TABLE_NOTICE

if TYPE_CHECKING:
    from syntera.drawings.graph import DrawingGraph, GraphNode

Mode = Literal["plain", "engineer"]

ASSURANCE_NOTE = (
    "This explains how the value was chosen. Whether the design passes is decided only by "
    "the exact geometry checks, never by an explanation."
)
INFERENCE_NOTE = (
    "Inferred from the drawing, not stated in it: the drawing gives geometry and text, not "
    "the reasons behind them. Check it against the drawing's own data."
)
SYNTHETIC_NOTE = "Based on synthetic demonstration tables, not on a design code."
DIAMETER_TEXT = re.compile(
    r"(?:\bOD\b|Ø|%%C|\bDIA(?:METER)?\b)\s*[:=]?\s*(\d+(?:\.\d+)?)", re.IGNORECASE
)
NOMINAL_TEXT = re.compile(r"\bDN\s*(\d{1,4})\b", re.IGNORECASE)
_UNIT_SUFFIXES = ("_mm", "_m_per_s", "_m3_per_h", "_mpa", "_c", "_k")


class ExplainError(ValueError):
    """The target cannot be explained; the message says what is available instead."""


class Explanation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str
    subject: str
    mode: Mode
    value: RationaleValue | None
    unit: str | None
    source: str
    inferred: bool
    headline: str
    steps: list[str]
    rule_id: str | None
    knowledge: list[str]
    citations: list[str]
    caveats: list[str]
    related: list[str]


def _entries(kb: KnowledgeBase, rule_id: str | None) -> list[KnowledgeEntry]:
    return kb.for_rule(rule_id) if rule_id else []


def _citations(entries: list[KnowledgeEntry]) -> list[str]:
    seen: list[str] = []
    for entry in entries:
        for citation in entry.citations:
            text = f"[{citation.kind}] {citation.source}"
            if text not in seen:
                seen.append(text)
    return seen


def _field(name: str) -> str:
    for suffix in _UNIT_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _resolve_path(design: DesignDocument, path: str) -> Rationale:
    records = {record.path: record for record in design.rationale}
    candidates = [path]
    if not path.startswith(("parameters.", "lines.")):
        candidates += [f"parameters.{path}"] + [f"lines.{line.id}.{path}" for line in design.lines]
    found = [records[c] for c in candidates if c in records]
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        raise ExplainError(f"{path} is ambiguous: {[record.path for record in found]}")
    index = build_index(load_knowledge(), [("design", design.rationale)])
    hits = index.search(path.replace(".", " ").replace("_", " "), 3, kinds=["rationale"])
    suggestion = ", ".join(hit.target for hit in hits) or "none"
    raise ExplainError(f"no rationale for {path}; closest: {suggestion}")


def _related(design: DesignDocument, record: Rationale) -> list[str]:
    """Other records in the same scope whose values this record's rule used as inputs."""
    scope = record.path.rsplit(".", 1)[0]
    while scope.count(".") > 1:
        scope = scope.rsplit(".", 1)[0]
    fields = {_field(name) for name in record.inputs}
    related = []
    for other in design.rationale:
        if other.path == record.path or not other.path.startswith(scope + "."):
            continue
        if other.path.rsplit(".", 1)[-1] in fields:
            related.append(other.path)
    return sorted(related)


def _format_input(name: str, value: RationaleValue) -> str:
    return f"{name} = {value:g}" if isinstance(value, float) else f"{name} = {value}"


def explain_value(
    design: DesignDocument, path: str, mode: Mode = "plain", kb: KnowledgeBase | None = None
) -> Explanation:
    """Why the design value at ``path`` (a rationale path, or its short form) has its value."""
    kb = kb or load_knowledge()
    record = _resolve_path(design, path)
    entries = _entries(kb, record.rule_id)
    background = False
    if not entries:
        index = build_index(kb)
        words = record.path.split(".", 2)[-1].replace(".", " ").replace("_", " ")
        hits = index.search(words, 1)
        if hits:
            entries = [kb.get(hits[0].target)]
            background = True
    caveats: list[str] = []
    if record.source == "default":
        caveats.append("The input did not state this value; the schema default was used.")
    if record.source == "inferred":
        caveats.append(INFERENCE_NOTE)
    synthetic = SYNTHETIC_TABLE_NOTICE in record.references or any(
        entry.synthetic for entry in entries if not background
    )
    if synthetic:
        caveats.append(SYNTHETIC_NOTE)
    caveats.append(ASSURANCE_NOTE)

    unit = f" {record.unit}" if record.unit else ""
    if mode == "plain":
        headline = record.text()
        steps = [entry.summary for entry in entries]
        if background:
            steps = [f"Background: {step}" for step in steps]
    else:
        rule = f", rule {record.rule_id}" if record.rule_id else ""
        headline = f"{record.path} = {record.value}{unit} (source: {record.source}{rule})"
        steps = [_format_input(name, value) for name, value in sorted(record.inputs.items())]
        steps.append(record.text())
        steps += [("Background: " if background else "") + entry.detail for entry in entries]
        steps += [f"Reference: {reference}" for reference in record.references]
    return Explanation(
        target=record.path,
        subject=design.title,
        mode=mode,
        value=record.value,
        unit=record.unit,
        source=record.source,
        inferred=record.source == "inferred",
        headline=headline,
        steps=steps,
        rule_id=record.rule_id,
        knowledge=[entry.id for entry in entries],
        citations=_citations(entries),
        caveats=caveats,
        related=_related(design, record),
    )


# ---- foreign drawings: always inference ----------------------------------------------------


def _node(graph: DrawingGraph, handle: str) -> GraphNode:
    try:
        return graph.node(handle)
    except KeyError as error:
        raise ExplainError(f"no drawing entity with handle {handle}") from error


def explain_entity(
    graph: DrawingGraph, handle: str, drawing: str, mode: Mode = "plain"
) -> Explanation:
    """What a drawing entity appears to be, from its labels and dimensions (inference)."""
    node = _node(graph, handle)
    labels = {label.handle: label for label in graph.labels}
    attached = [labels[h] for h in node.labels if h in labels]
    dims = {dimension.handle: dimension for dimension in graph.dimensions}
    measured = [dims[h] for h in node.dimensions if h in dims]
    names = ", ".join(f"'{label.text}'" for label in attached) or "no label"
    headline = f"Inferred: {node.type} {handle} on layer {node.layer} is a {node.kind} ({names})."
    steps = []
    for label in attached:
        where = "inside its outline" if label.distance == 0 else f"{label.distance:g} units away"
        steps.append(f"Label '{label.text}' is the nearest text ({where}).")
    for dimension in measured:
        value = "?" if dimension.measurement is None else f"{dimension.measurement:g}"
        steps.append(f"A {dimension.kind} dimension of {value} touches it ({dimension.handle}).")
    if mode == "engineer":
        (x0, y0), (x1, y1) = node.bbox
        steps.append(f"Bounding box ({x0:g}, {y0:g}) to ({x1:g}, {y1:g}) in drawing units.")
        steps.append(f"Kind from {'design reference ' + node.ref if node.ref else 'layer name'}.")
    return Explanation(
        target=f"drawing:{drawing}:{handle}",
        subject=drawing,
        mode=mode,
        value=None,
        unit=None,
        source="inferred",
        inferred=True,
        headline=headline,
        steps=steps,
        rule_id=None,
        knowledge=[],
        citations=[],
        caveats=[INFERENCE_NOTE, ASSURANCE_NOTE],
        related=sorted(h for h in node.labels + node.dimensions),
    )


def stated_diameter(graph: DrawingGraph, handle: str) -> tuple[float, str, bool]:
    """Diameter (drawing units) stated for an entity, how it was found, and whether nominal."""
    node = _node(graph, handle)
    dims = {dimension.handle: dimension for dimension in graph.dimensions}
    for h in node.dimensions:
        dimension = dims.get(h)
        if dimension is None or dimension.measurement is None:
            continue
        if dimension.kind == "diameter":
            return dimension.measurement, f"diameter dimension {h}", False
        if dimension.kind == "radius":
            return 2 * dimension.measurement, f"radius dimension {h} (doubled)", False
    labels = {label.handle: label for label in graph.labels}
    for h in node.labels:
        label = labels.get(h)
        if label is None:
            continue
        match = DIAMETER_TEXT.search(label.text)
        if match:
            return float(match.group(1)), f"label '{label.text}'", False
        match = NOMINAL_TEXT.search(label.text)
        if match:
            from syntera.pid.layout import outside_diameter

            nominal = int(match.group(1))
            return outside_diameter(nominal), f"label '{label.text}' (DN{nominal})", True
    raise ExplainError(f"no diameter is stated for {handle}: no diameter dimension or OD label")


def back_calculate_velocity(
    graph: DrawingGraph,
    handle: str,
    drawing: str,
    flow_m3_per_h: float,
    wall_mm: float | None = None,
    mm_per_unit: float = 1.0,
    mode: Mode = "plain",
    kb: KnowledgeBase | None = None,
) -> Explanation:
    """Flow velocity in a drawn line from its stated diameter and a given flow (inference)."""
    kb = kb or load_knowledge()
    diameter_units, found, nominal = stated_diameter(graph, handle)
    outside = diameter_units * mm_per_unit
    bore = outside - 2 * wall_mm if wall_mm is not None else outside
    try:
        result = flow_velocity(flow_m3_per_h, bore)
    except RuleError as error:
        raise ExplainError(str(error)) from error
    record = result.records[0]
    caveats = [INFERENCE_NOTE]
    if wall_mm is None:
        caveats.append("No wall thickness was given, so the outside diameter is used as the "
                       "bore; the true velocity is higher.")  # fmt: skip
    if nominal:
        caveats.append("The size is nominal (DN); its outside diameter comes from a table.")
    caveats.append(ASSURANCE_NOTE)
    entries = _entries(kb, VELOCITY_RULE)
    headline = (
        f"Inferred: about {result.value:g} m/s, from {flow_m3_per_h:g} m3/h through a "
        f"{bore:g} mm bore taken from the {found}."
    )
    steps = [f"Outside diameter {outside:g} mm from the {found}."]
    if wall_mm is not None:
        steps.append(f"Bore = {outside:g} - 2 x {wall_mm:g} = {bore:g} mm.")
    steps.append(record.text())
    if mode == "engineer":
        steps.append("v = Q / A, A = pi d^2 / 4, Q in m3/s, d in m.")
        steps += [entry.detail for entry in entries]
    else:
        steps += [entry.summary for entry in entries]
    return Explanation(
        target=f"drawing:{drawing}:{handle}:velocity",
        subject=drawing,
        mode=mode,
        value=result.value,
        unit="m/s",
        source="inferred",
        inferred=True,
        headline=headline,
        steps=steps,
        rule_id=VELOCITY_RULE,
        knowledge=[entry.id for entry in entries],
        citations=_citations(entries),
        caveats=caveats,
        related=[],
    )
