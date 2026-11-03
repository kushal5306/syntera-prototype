"""Typed change sets for design documents, their exact inverses, and design diffs (A4).

A :class:`ChangeSet` is a list of typed edits: set a parameter, move equipment (its
nozzles move with it), add a line or remove a line. :func:`apply_change_set` validates the
whole result as a design document and returns it together with the inverse change set
that restores the original byte for byte. Stored routes are cleared by any change, because
they no longer describe the design; the deterministic pipeline regenerates them.

:func:`diff_designs` lists changed values and changed rationale, and
:func:`verdict_changes` lists the checks whose verdict differs between two runs. Change
sets edit design inputs only: nothing here can write or override an assurance verdict.
"""

from __future__ import annotations

import copy
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from syntera.design import DesignDocument, DesignError, Line, Nozzle, RouteRecord, parse_design
from syntera.multiline import DesignRunReport
from syntera.rationale import Rationale, unit_for

CHANGES_FILE = "changes.json"
REVERT_FILE = "revert_changes.json"
DIFF_FILE = "design_diff.json"

# Roots a set-parameter edit may change; ids, versions, provenance and outputs are fixed.
SETTABLE_ROOTS = frozenset({"title", "parameters", "lines", "equipment", "nozzles"})
_FIXED_LEAVES = frozenset({"id", "type", "name", "from_nozzle", "to_nozzle", "equipment"})
# Rationale paths exist only for design values (see syntera.design).
_RATIONALE_ROOTS = frozenset({"parameters", "lines"})

ParameterValue = bool | int | float | str | list[float] | None


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SetParameter(StrictModel):
    """Set one value at a design path, e.g. ``lines.main.tube.minimum_clearance``.

    Values under ``parameters.`` and ``lines.`` get a ``user`` rationale record (with
    ``explanation`` when given) replacing any record at the same path.
    """

    op: Literal["set_parameter"] = "set_parameter"
    path: str = Field(pattern=r"^[A-Za-z0-9_\-]+(\.[A-Za-z0-9_\-]+)+$|^title$")
    value: ParameterValue
    explanation: str | None = Field(default=None, min_length=1, max_length=500)


class SetRationale(StrictModel):
    """Replace (or with ``record`` of ``None`` remove) the rationale record at ``path``.

    Inverse change sets use it to restore records exactly.
    """

    op: Literal["set_rationale"] = "set_rationale"
    path: str
    record: Rationale | None


class MoveEquipment(StrictModel):
    """Move one item of equipment, and every nozzle on it, by ``offset`` (mm)."""

    op: Literal["move_equipment"] = "move_equipment"
    equipment: str = Field(min_length=1)
    offset: tuple[float, float, float]


class IndexedNozzle(StrictModel):
    index: int | None = Field(default=None, ge=0)
    nozzle: Nozzle


class AddLine(StrictModel):
    """Add a line, the new nozzles it needs, and optionally its rationale records."""

    op: Literal["add_line"] = "add_line"
    line: Line
    nozzles: list[IndexedNozzle] = Field(default_factory=list)
    index: int | None = Field(default=None, ge=0)
    rationale: list[Rationale] = Field(default_factory=list)


class RemoveLine(StrictModel):
    """Remove a line and its rationale, plus nozzles: those named in ``nozzles``, or by
    default the line's own nozzles that no other line uses."""

    op: Literal["remove_line"] = "remove_line"
    line: str = Field(min_length=1)
    nozzles: list[str] | None = None


class SetRoutes(StrictModel):
    """Restore stored routes (used by inverse change sets)."""

    op: Literal["set_routes"] = "set_routes"
    routes: list[RouteRecord]


Edit = Annotated[
    SetParameter | SetRationale | MoveEquipment | AddLine | RemoveLine | SetRoutes,
    Field(discriminator="op"),
]


class ChangeSet(StrictModel):
    description: str = Field(min_length=1, max_length=500)
    edits: list[Edit] = Field(min_length=1)


# ---- applying ---------------------------------------------------------------------------


def _locate(data: dict[str, Any], path: str) -> tuple[Any, str | int]:
    """Parent container and key for ``path``; lists of objects are indexed by ``id``."""
    segments = path.split(".")
    if segments[0] not in SETTABLE_ROOTS:
        raise DesignError(f"{path}: only {sorted(SETTABLE_ROOTS)} can be changed")
    if segments[-1] in _FIXED_LEAVES:
        raise DesignError(f"{path}: ids and references cannot be set; add or remove instead")
    node: Any = data
    for depth, segment in enumerate(segments[:-1]):
        node = _child(node, segment, ".".join(segments[: depth + 1]))
    last = segments[-1]
    if isinstance(node, dict):
        if last not in node:
            raise DesignError(f"{path}: no such field")
        return node, last
    raise DesignError(f"{path}: not a field of an object")


def _child(node: Any, segment: str, where: str) -> Any:
    if isinstance(node, dict):
        if segment not in node or node[segment] is None:
            raise DesignError(f"{where}: no such field")
        return node[segment]
    if isinstance(node, list):
        for item in node:
            if isinstance(item, dict) and item.get("id") == segment:
                return item
        raise DesignError(f"{where}: no item with id {segment}")
    raise DesignError(f"{where}: not an object")


def _get(data: dict[str, Any], path: str) -> Any:
    parent, key = _locate(data, path)
    return copy.deepcopy(parent[key])


def _set(data: dict[str, Any], path: str, value: Any) -> None:
    parent, key = _locate(data, path)
    parent[key] = copy.deepcopy(value)


def _records(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {record["path"]: record for record in data["rationale"]}


def _put_record(data: dict[str, Any], path: str, record: dict[str, Any] | None) -> None:
    records = _records(data)
    records.pop(path, None)
    if record is not None:
        records[path] = record
    data["rationale"] = [records[key] for key in sorted(records)]


def _user_record(path: str, value: Any, explanation: str | None) -> dict[str, Any] | None:
    """A ``user`` record for a design value set by a change set (none for geometry)."""
    if path.split(".")[0] not in _RATIONALE_ROOTS:
        return None
    if isinstance(value, list):
        value = tuple(float(v) for v in value)
    if value is None or not isinstance(value, bool | int | float | str | tuple):
        return None
    template = "{value} {unit} set by a confirmed change set."
    if explanation:
        reason = explanation.replace("{", "{{").replace("}", "}}")
        template = f"{reason} ({{value}} {{unit}}, set by a confirmed change set)"
    record = Rationale(
        path=path,
        value=value,
        unit=unit_for(path.rsplit(".", 1)[-1]),
        source="user",
        explanation=template,
    )
    return record.model_dump(mode="json")


def _apply_set(data: dict[str, Any], edit: SetParameter) -> list[Edit]:
    old_value = _get(data, edit.path)
    if isinstance(old_value, dict) or (
        isinstance(old_value, list) and any(isinstance(item, dict) for item in old_value)
    ):
        raise DesignError(f"{edit.path}: not a single value; set its fields one at a time")
    old_record = _records(data).get(edit.path)
    _set(data, edit.path, edit.value)
    record = _user_record(edit.path, edit.value, edit.explanation)
    if record is not None or old_record is not None:
        _put_record(data, edit.path, record)
    inverse: list[Edit] = [SetParameter(path=edit.path, value=old_value)]
    if record is not None or old_record is not None:
        restored = None if old_record is None else Rationale.model_validate(old_record)
        inverse.append(SetRationale(path=edit.path, record=restored))
    return inverse


def _apply_move(data: dict[str, Any], edit: MoveEquipment) -> list[Edit]:
    item = _child(data["equipment"], edit.equipment, f"equipment.{edit.equipment}")
    geometry = item["geometry"]
    field = "translation" if geometry["type"] == "step" else "center"
    inverse: list[Edit] = []
    path = f"equipment.{edit.equipment}.geometry.{field}"
    old = list(geometry[field])
    inverse.append(SetParameter(path=path, value=old))
    geometry[field] = [old[i] + edit.offset[i] for i in range(3)]
    for nozzle in data["nozzles"]:
        if nozzle.get("equipment") == edit.equipment:
            position = list(nozzle["port"]["position"])
            inverse.append(
                SetParameter(path=f"nozzles.{nozzle['id']}.port.position", value=position)
            )
            nozzle["port"]["position"] = [position[i] + edit.offset[i] for i in range(3)]
    return inverse


def _insert(items: list[Any], index: int | None, value: Any) -> None:
    items.insert(len(items) if index is None else min(index, len(items)), value)


def _apply_add(data: dict[str, Any], edit: AddLine) -> list[Edit]:
    if any(line["id"] == edit.line.id for line in data["lines"]):
        raise DesignError(f"line {edit.line.id} already exists")
    for entry in edit.nozzles:
        if any(nozzle["id"] == entry.nozzle.id for nozzle in data["nozzles"]):
            raise DesignError(f"nozzle {entry.nozzle.id} already exists")
        _insert(data["nozzles"], entry.index, entry.nozzle.model_dump(mode="json"))
    _insert(data["lines"], edit.index, edit.line.model_dump(mode="json"))
    prefix = f"lines.{edit.line.id}."
    for record in edit.rationale:
        if not record.path.startswith(prefix):
            raise DesignError(f"rationale {record.path} does not belong to line {edit.line.id}")
        _put_record(data, record.path, record.model_dump(mode="json"))
    return [RemoveLine(line=edit.line.id, nozzles=[entry.nozzle.id for entry in edit.nozzles])]


def _apply_remove(data: dict[str, Any], edit: RemoveLine) -> list[Edit]:
    lines = data["lines"]
    index = next((i for i, line in enumerate(lines) if line["id"] == edit.line), None)
    if index is None:
        raise DesignError(f"unknown line {edit.line}")
    line = lines.pop(index)
    still_used = {end for other in lines for end in (other["from_nozzle"], other["to_nozzle"])}
    if edit.nozzles is None:
        doomed = {line["from_nozzle"], line["to_nozzle"]} - still_used
    else:
        doomed = set(edit.nozzles)
        if doomed & still_used:
            raise DesignError(f"nozzles {sorted(doomed & still_used)} are used by other lines")
    removed: list[IndexedNozzle] = []
    kept = []
    for position, nozzle in enumerate(data["nozzles"]):
        if nozzle["id"] in doomed:
            removed.append(IndexedNozzle(index=position, nozzle=Nozzle.model_validate(nozzle)))
        else:
            kept.append(nozzle)
    data["nozzles"] = kept
    prefix = f"lines.{edit.line}."
    records = [
        Rationale.model_validate(r) for r in data["rationale"] if r["path"].startswith(prefix)
    ]
    data["rationale"] = [r for r in data["rationale"] if not r["path"].startswith(prefix)]
    return [
        AddLine(line=Line.model_validate(line), nozzles=removed, index=index, rationale=records)
    ]


def _apply_edit(data: dict[str, Any], edit: Edit) -> list[Edit]:
    if isinstance(edit, SetParameter):
        return _apply_set(data, edit)
    if isinstance(edit, SetRationale):
        old = _records(data).get(edit.path)
        record = None if edit.record is None else edit.record.model_dump(mode="json")
        _put_record(data, edit.path, record)
        restored = None if old is None else Rationale.model_validate(old)
        return [SetRationale(path=edit.path, record=restored)]
    if isinstance(edit, MoveEquipment):
        return _apply_move(data, edit)
    if isinstance(edit, AddLine):
        return _apply_add(data, edit)
    if isinstance(edit, RemoveLine):
        return _apply_remove(data, edit)
    old_routes = [RouteRecord.model_validate(route) for route in data["routes"]]
    data["routes"] = [route.model_dump(mode="json") for route in edit.routes]
    return [SetRoutes(routes=old_routes)]


def apply_change_set(
    design: DesignDocument, changes: ChangeSet
) -> tuple[DesignDocument, ChangeSet]:
    """The changed design and the change set that reverts it exactly.

    Edits apply in order; the inverse lists the undo edits in reverse order. Unless the
    change set itself sets routes, stored routes are cleared (and restored by the inverse).
    """
    data = design.model_dump(mode="json")
    undo: list[list[Edit]] = []
    for number, edit in enumerate(changes.edits, start=1):
        try:
            undo.append(_apply_edit(data, edit))
        except DesignError as error:
            raise DesignError(f"edit {number} ({edit.op}): {error}") from error
    if not any(isinstance(edit, SetRoutes) for edit in changes.edits) and data["routes"]:
        undo.append(_apply_edit(data, SetRoutes(routes=[])))
    try:
        changed = parse_design(data)
    except ValidationError as error:
        raise DesignError(f"the change set gives an invalid design: {error}") from error
    inverse = [edit for group in reversed(undo) for edit in group]
    revert = ChangeSet(description=f"Revert: {changes.description}"[:500], edits=inverse)
    return changed, revert


# ---- diffs ------------------------------------------------------------------------------


class ValueChange(StrictModel):
    path: str
    before: Any = None
    after: Any = None


class RationaleChange(StrictModel):
    path: str
    before: Rationale | None
    after: Rationale | None


class VerdictChange(StrictModel):
    check: str
    before: bool | None
    after: bool | None


class DesignDiff(StrictModel):
    """Changed values, changed rationale and (after regeneration) changed verdicts."""

    before_title: str
    after_title: str
    values: list[ValueChange]
    rationale: list[RationaleChange]
    routes_changed: list[str]
    verdicts: list[VerdictChange] = Field(default_factory=list)
    regenerated: bool = False


_SKIP_ROOTS = ("rationale", "routes", "inputs")


def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """Leaf values by design path; lists of objects with ids are keyed by id."""
    if isinstance(value, dict):
        leaves: dict[str, Any] = {}
        for key in sorted(value):
            if not prefix and key in _SKIP_ROOTS:
                continue
            leaves.update(flatten(value[key], f"{prefix}.{key}" if prefix else key))
        return leaves
    if (
        isinstance(value, list)
        and value
        and all(isinstance(item, dict) and "id" in item for item in value)
    ):
        leaves = {}
        for item in value:
            leaves.update(flatten(item, f"{prefix}.{item['id']}"))
        return leaves
    return {prefix: value}


def diff_designs(before: DesignDocument, after: DesignDocument) -> DesignDiff:
    old, new = flatten(before.model_dump(mode="json")), flatten(after.model_dump(mode="json"))
    values = [
        ValueChange(path=path, before=old.get(path), after=new.get(path))
        for path in sorted(old.keys() | new.keys())
        if old.get(path) != new.get(path) or (path in old) != (path in new)
    ]
    old_records = {record.path: record for record in before.rationale}
    new_records = {record.path: record for record in after.rationale}
    rationale = [
        RationaleChange(path=path, before=old_records.get(path), after=new_records.get(path))
        for path in sorted(old_records.keys() | new_records.keys())
        if old_records.get(path) != new_records.get(path)
    ]
    old_routes = {route.line: route for route in before.routes}
    new_routes = {route.line: route for route in after.routes}
    routes = sorted(
        line
        for line in old_routes.keys() | new_routes.keys()
        if old_routes.get(line) != new_routes.get(line)
    )
    return DesignDiff(
        before_title=before.title,
        after_title=after.title,
        values=values,
        rationale=rationale,
        routes_changed=routes,
    )


def verdicts(report: DesignRunReport) -> dict[str, bool]:
    """Every check verdict in a design run, by a stable check name."""
    result = {"overall": report.overall_pass}
    for line in report.lines:
        result[f"line:{line.line}"] = line.overall_pass
    for pair in report.pairs:
        result[f"pair:{pair.lines[0]}/{pair.lines[1]}"] = pair.passed
    return result


def verdict_changes(before: DesignRunReport, after: DesignRunReport) -> list[VerdictChange]:
    old, new = verdicts(before), verdicts(after)
    return [
        VerdictChange(check=check, before=old.get(check), after=new.get(check))
        for check in sorted(old.keys() | new.keys())
        if old.get(check) != new.get(check)
    ]
