"""Read equipment and piping connectivity from a DEXPI (Proteus XML) P&ID.

A P&ID records what connects to what, not where anything is, so only tags, classes,
size attributes and the connection topology are read. Graphics positions are ignored.
"""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from syntera.pid.models import PidConnection, PidDocument, PidEquipment, PidNozzle, Terminal

MAX_XML_BYTES = 20 * 1024 * 1024
SIZE_ATTRIBUTES = {
    "NominalDiameter",
    "CylinderLength",
    "NominalCapacity(Volume)",
    "TubeLength",
    "PlateHeight",
    "PlateWidth",
    "Length",
    "Height",
    "Width",
    "InsideDiameter",
    "OutsideDiameter",
}
UNIT_TO_MM = {None: 1.0, "": 1.0, "Millimetre": 1.0, "Centimetre": 10.0, "Metre": 1000.0}


DEAD_ENDS = {"BlindFlange", "PipeCap", "Cap", "Plug", "PipePlug"}


class PidError(ValueError):
    """The file is not a DEXPI P&ID Syntera can read."""


def _attributes(element: ET.Element) -> dict[str, tuple[str, str | None]]:
    return {
        str(item.get("Name")): (str(item.get("Value") or ""), item.get("Units"))
        for item in element.findall("./GenericAttributes/GenericAttribute")
        if item.get("Name")
    }


def _tag(element: ET.Element) -> str:
    attributes = _attributes(element)
    for key in ("TagNameAssignmentClass", "SubTagNameAssignmentClass"):
        value = attributes.get(key, ("", None))[0].strip()
        if value:
            return value
    return element.get("TagName") or element.get("ID") or "?"


def _sizes(element: ET.Element) -> dict[str, float]:
    """Size attributes of an item and its sub-equipment (chambers, tube bundles), in mm."""
    sizes: dict[str, float] = {}
    for item in [element, *element.iter("Equipment")]:
        for name, (value, unit) in _attributes(item).items():
            if name not in SIZE_ATTRIBUTES:
                continue
            try:
                number = float(value)
            except ValueError:
                continue
            if name == "NominalCapacity(Volume)":
                factor = {"MetreCubed": 1e9, "Litre": 1e6}.get(unit or "")
            else:
                factor = UNIT_TO_MM.get(unit)
            if factor is None or number <= 0:
                continue
            sizes.setdefault(name, number * factor)
    return sizes


def _nominal_diameter(element: ET.Element) -> int | None:
    value = _attributes(element).get("NominalDiameterNumericalValueRepresentationAssignmentClass")
    try:
        return int(float(value[0])) if value else None
    except ValueError:
        return None


def read_dexpi(data: bytes, filename: str) -> PidDocument:
    """Parse DEXPI XML into equipment and nozzle-to-nozzle connections."""
    if len(data) > MAX_XML_BYTES:
        raise PidError("P&ID file exceeds the 20 MB limit")
    if b"<!DOCTYPE" in data[:4096] or b"<!ENTITY" in data[:65536]:
        raise PidError("XML document type declarations are not accepted")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        raise PidError(f"not readable XML: {error}") from error
    if root.tag != "PlantModel":
        raise PidError("not a DEXPI P&ID (the root element is not PlantModel)")
    info = root.find("PlantInformation")

    equipment: list[PidEquipment] = []
    skipped: list[str] = []
    nozzle_owner: dict[str, str] = {}
    labels: dict[str, str] = {}
    for item in root.findall("Equipment"):
        component = item.get("ComponentClass")
        nozzles = [PidNozzle(id=n.get("ID") or "", tag=_tag(n)) for n in item.findall("Nozzle")]
        if not component or not item.get("ID"):
            continue
        if not nozzles:
            skipped.append(f"{_tag(item)} ({component}) has no nozzles; not placed")
            continue
        tag = _tag(item)
        equipment.append(
            PidEquipment(
                id=item.get("ID") or "",
                tag=tag,
                component_class=component,
                nozzles=nozzles,
                attributes=_sizes(item),
            )
        )
        for nozzle in nozzles:
            nozzle_owner[nozzle.id] = item.get("ID") or ""
            labels[nozzle.id] = f"{tag} {nozzle.tag}"

    connections: list[PidConnection] = []
    found, notes = _connections(root, nozzle_owner, labels)
    connections.extend(found)
    skipped.extend(notes)
    if not equipment and not connections:
        raise PidError("the P&ID lists no equipment and no complete pipe line")
    return PidDocument(
        source_sha256=hashlib.sha256(data).hexdigest(),
        filename=Path(filename.replace("\\", "/")).name[:120] or "pid.xml",
        originating_system=info.get("OriginatingSystem") if info is not None else None,
        equipment=equipment,
        connections=connections,
        skipped=skipped,
    )


@dataclass(frozen=True)
class _Edge:
    line: str
    diameter: int | None


def _connections(
    root: ET.Element, nozzle_owner: dict[str, str], labels: dict[str, str]
) -> tuple[list[PidConnection], list[str]]:
    """Reduce the plant's piping graph to the runs between its terminals.

    All lines form one graph, since a line number can change mid-run. Inline items
    (valves, reducers, tees) are passed through and dead-end branches (blind flanges,
    drains) are dropped. A network with more than two terminals is treated as a header:
    its terminals are joined in the order a walk along it meets them.
    """
    graph: dict[str, dict[str, _Edge]] = defaultdict(dict)
    inbound: set[str] = set()
    classes: dict[str, str] = {}
    for system in root.iter("PipingNetworkSystem"):
        line = _attributes(system).get("LineNumberAssignmentClass", ("", None))[0] or (
            system.get("ID") or "line"
        )
        system_diameter = _nominal_diameter(system)
        for item in system.iter():
            if item.get("ID"):
                classes[item.get("ID") or ""] = item.get("ComponentClass") or item.tag
        for index, segment in enumerate(system.findall("PipingNetworkSegment")):
            connection = segment.find("Connection")
            if connection is None:
                continue
            name = segment.get("ID") or f"{line}-{index}"
            start = connection.get("FromID") or f"{name}:open-start"
            end = connection.get("ToID") or f"{name}:open-end"
            if connection.get("FromID") is None:
                inbound.add(start)
            if start == end:
                continue
            edge = _Edge(line, _nominal_diameter(segment) or system_diameter)
            graph[start][end] = edge
            graph[end][start] = edge
    offpage = {node for node in graph if "OffPageConnector" in classes.get(node, "")}
    inbound |= {node for node in offpage if "FlowIn" in classes.get(node, "")}
    terminals = {
        node
        for node in graph
        if node in nozzle_owner or node in offpage or node.endswith((":open-start", ":open-end"))
    }

    connections: list[PidConnection] = []
    notes: list[str] = []
    for component in _components(graph):
        present = sorted(node for node in component if node in terminals)
        if len(present) < 2:
            # The run continues off this sheet: its far ends become battery limits,
            # unless an end is blanked off (then it is a closed stub and is not routed).
            ends = _far_ends(graph, component)
            if any(classes.get(end) in DEAD_ENDS for end in ends) or ends[0] == ends[1]:
                if present:
                    notes.append(f"{labels.get(present[0], present[0])} leads to a blanked stub")
                continue
            terminals |= set(ends)
            if not any(end in inbound for end in ends):
                inbound.add(ends[0])
        found, extra = _runs(graph, component, terminals, inbound, nozzle_owner, labels)
        connections.extend(found)
        notes.extend(extra)
    return connections, notes


def _components(graph: dict[str, dict[str, _Edge]]) -> list[set[str]]:
    seen: set[str] = set()
    parts: list[set[str]] = []
    for start in sorted(graph):
        if start in seen:
            continue
        part, queue = {start}, [start]
        for node in queue:
            for neighbour in sorted(graph[node]):
                if neighbour not in part:
                    part.add(neighbour)
                    queue.append(neighbour)
        seen |= part
        parts.append(part)
    return parts


def _runs(
    graph: dict[str, dict[str, _Edge]],
    component: set[str],
    terminals: set[str],
    inbound: set[str],
    nozzle_owner: dict[str, str],
    labels: dict[str, str],
) -> tuple[list[PidConnection], list[str]]:
    keep = set(component)
    pruned = True
    while pruned:  # drop dead-end branches that reach no terminal
        pruned = False
        for node in sorted(keep):
            if node not in terminals and sum(n in keep for n in graph[node]) <= 1:
                keep.discard(node)
                pruned = True
    present = sorted(node for node in keep if node in terminals)
    start = next((node for node in present if node in inbound), present[0])
    order: list[str] = []
    parent: dict[str, str | None] = {start: None}
    stack = [start]
    while stack:
        node = stack.pop()
        if node in terminals:
            order.append(node)
        for neighbour in sorted((n for n in graph[node] if n in keep), reverse=True):
            if neighbour not in parent:
                parent[neighbour] = node
                stack.append(neighbour)

    def root_path(node: str) -> list[str]:
        path = [node]
        while parent[path[-1]] is not None:
            path.append(parent[path[-1]])  # type: ignore[arg-type]
        return path[::-1]

    def terminal(node: str) -> Terminal:
        if node in nozzle_owner:
            return Terminal(
                id=node, kind="nozzle", equipment_id=nozzle_owner[node], label=labels[node]
            )
        side = "inlet" if node in inbound else "outlet"
        return Terminal(
            id=node, kind="battery_limit", label=f"{side} at skid edge", inbound=node in inbound
        )

    runs: list[PidConnection] = []
    notes: list[str] = []
    header = len(order) > 2
    for a, b in zip(order, order[1:], strict=False):
        pa, pb = root_path(a), root_path(b)
        common = 0
        while common < min(len(pa), len(pb)) and pa[common] == pb[common]:
            common += 1
        path = pa[common - 1 :][::-1] + pb[common:]  # a ... lowest common node ... b
        edges = [graph[x][y] for x, y in zip(path, path[1:], strict=False)]
        lines = list(dict.fromkeys(edge.line for edge in edges))
        sizes = [edge.diameter for edge in edges if edge.diameter]
        name = "/".join(lines)
        if nozzle_owner.get(a) and nozzle_owner.get(a) == nozzle_owner.get(b):
            notes.append(f"line {name}: {labels[a]} to {labels[b]} is on one item; not routed")
            continue
        start_terminal, end_terminal = terminal(a), terminal(b)
        if start_terminal.kind == "battery_limit":
            start_terminal = start_terminal.model_copy(
                update={"label": f"line {name} {start_terminal.label}"}
            )
        if end_terminal.kind == "battery_limit":
            end_terminal = end_terminal.model_copy(
                update={"label": f"line {name} {end_terminal.label}"}
            )
        runs.append(
            PidConnection(
                id=f"{lines[0]}-{len(runs) + 1}" if lines else f"run-{len(runs) + 1}",
                line=name,
                nominal_diameter=max(sizes) if sizes else None,
                start=start_terminal,
                end=end_terminal,
                via=[node for node in path[1:-1]],
                note="part of a header with branches" if header else "",
            )
        )
    return runs, notes


def _far_ends(graph: dict[str, dict[str, _Edge]], component: set[str]) -> tuple[str, str]:
    """The two nodes furthest apart along the run (a double breadth-first sweep)."""

    def farthest(start: str) -> str:
        depth = {start: 0}
        queue = [start]
        for node in queue:
            for neighbour in sorted(graph[node]):
                if neighbour not in depth:
                    depth[neighbour] = depth[node] + 1
                    queue.append(neighbour)
        return max(sorted(depth), key=lambda node: depth[node])

    first = farthest(sorted(component)[0])
    return first, farthest(first)
