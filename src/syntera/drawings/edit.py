"""Round-trip editing of an imported DXF with a typed change set (roadmap D4).

A :class:`DrawingChangeSet` moves entities, changes their layer, replaces a text, or
renames a tag wherever it appears in labels and attributes. Edits are applied to the
DXF as read (no recovery rewrite), only the named entities change, and the result is
written back as DXF (and as DWG when the ODA File Converter is installed). The edit
report lists every changed handle and fingerprints every entity, so untouched entities
can be shown to be byte-stable.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Annotated, Literal

import ezdxf
from ezdxf.document import Drawing
from ezdxf.entities import DXFEntity
from ezdxf.lldxf.tagwriter import TagCollector
from ezdxf.math import Matrix44
from pydantic import BaseModel, ConfigDict, Field

from syntera.drawings.convert import DrawingError

EDITED_DXF = "edited.dxf"
EDIT_REPORT = "drawing_edit.json"
TEXT_TYPES = frozenset({"TEXT", "MTEXT"})


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MoveEntity(StrictModel):
    op: Literal["move"] = "move"
    handle: str = Field(min_length=1)
    offset: tuple[float, float, float]


class SetLayer(StrictModel):
    op: Literal["set_layer"] = "set_layer"
    handle: str = Field(min_length=1)
    layer: str = Field(min_length=1, max_length=255, pattern=r"^[^<>/\\\":;?*|=`]+$")


class SetText(StrictModel):
    op: Literal["set_text"] = "set_text"
    handle: str = Field(min_length=1)
    text: str = Field(max_length=2000)


class Retag(StrictModel):
    """Rename a tag (e.g. ``P-101`` to ``P-201``) in every text and attribute value."""

    op: Literal["retag"] = "retag"
    old: str = Field(min_length=1, max_length=80)
    new: str = Field(min_length=1, max_length=80)


DrawingEdit = Annotated[MoveEntity | SetLayer | SetText | Retag, Field(discriminator="op")]


class DrawingChangeSet(StrictModel):
    description: str = Field(min_length=1, max_length=500)
    edits: list[DrawingEdit] = Field(min_length=1)


class DrawingEditReport(StrictModel):
    description: str
    source_sha256: str
    output_sha256: str
    changed: list[str]
    untouched: int
    fingerprints_before: dict[str, str]
    fingerprints_after: dict[str, str]
    dwg: str | None = None


def entity_fingerprint(entity: DXFEntity, dxfversion: str) -> str:
    """SHA-256 of the entity's exported DXF tags (and its attributes for inserts)."""
    collector = TagCollector(dxfversion=dxfversion)
    entity.export_dxf(collector)
    if entity.dxftype() == "INSERT":
        for attrib in entity.attribs:
            attrib.export_dxf(collector)
    text = "\n".join(f"{tag.code}\n{tag.value}" for tag in collector.tags)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fingerprints(doc: Drawing) -> dict[str, str]:
    result = {}
    for layout in [doc.modelspace(), *(doc.layouts.get(n) for n in doc.layouts.names()
                                       if n != "Model")]:  # fmt: skip
        for entity in layout:
            result[entity.dxf.handle] = entity_fingerprint(entity, doc.dxfversion)
    return dict(sorted(result.items()))


def _entity(doc: Drawing, handle: str) -> DXFEntity:
    entity = doc.entitydb.get(handle.upper())
    if entity is None or not entity.is_alive:
        raise DrawingError(f"no entity with handle {handle}")
    return entity


def _tag_pattern(tag: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9-]){re.escape(tag)}(?![A-Za-z0-9-])")


def apply_drawing_changes(doc: Drawing, changes: DrawingChangeSet) -> list[str]:
    """Apply edits in order; return the handles of every entity that changed."""
    changed: list[str] = []

    def mark(handle: str) -> None:
        if handle not in changed:
            changed.append(handle)

    for number, edit in enumerate(changes.edits, start=1):
        try:
            if isinstance(edit, MoveEntity):
                entity = _entity(doc, edit.handle)
                entity.transform(Matrix44.translate(*edit.offset))
                mark(entity.dxf.handle)
            elif isinstance(edit, SetLayer):
                entity = _entity(doc, edit.handle)
                if edit.layer not in doc.layers:
                    doc.layers.add(edit.layer)
                entity.dxf.layer = edit.layer
                mark(entity.dxf.handle)
            elif isinstance(edit, SetText):
                entity = _entity(doc, edit.handle)
                if entity.dxftype() not in TEXT_TYPES:
                    raise DrawingError(f"{edit.handle} is a {entity.dxftype()}, not a text")
                if entity.dxftype() == "MTEXT":
                    entity.text = edit.text
                else:
                    entity.dxf.text = edit.text
                mark(entity.dxf.handle)
            else:
                pattern = _tag_pattern(edit.old)
                found = False
                for layout in [doc.modelspace(), *(doc.layouts.get(n) for n in
                               doc.layouts.names() if n != "Model")]:  # fmt: skip
                    for entity in layout:
                        kind = entity.dxftype()
                        if kind == "TEXT" and pattern.search(entity.dxf.text):
                            entity.dxf.text = pattern.sub(edit.new, entity.dxf.text)
                        elif kind == "MTEXT" and pattern.search(entity.text):
                            entity.text = pattern.sub(edit.new, entity.text)
                        elif kind == "INSERT" and any(
                            pattern.search(a.dxf.text) for a in entity.attribs
                        ):
                            for attrib in entity.attribs:
                                attrib.dxf.text = pattern.sub(edit.new, attrib.dxf.text)
                        else:
                            continue
                        found = True
                        mark(entity.dxf.handle)
                if not found:
                    raise DrawingError(f"tag {edit.old} does not appear in the drawing")
        except DrawingError as error:
            raise DrawingError(f"edit {number} ({edit.op}): {error}") from error
    return changed


def edit_dxf(
    source: Path, changes: DrawingChangeSet, output: Path, dwg: bool = False
) -> DrawingEditReport:
    """Read ``source``, apply ``changes``, write ``edited.dxf`` (and DWG if asked)."""
    data = source.read_bytes()
    try:
        doc = ezdxf.readfile(source)
    except (OSError, ezdxf.DXFStructureError) as error:
        raise DrawingError(f"{source.name} could not be read as DXF: {error}") from error
    before = fingerprints(doc)
    changed = apply_drawing_changes(doc, changes)
    output.mkdir(parents=True, exist_ok=True)
    target = output / EDITED_DXF
    doc.saveas(target)
    after = fingerprints(ezdxf.readfile(target))
    dwg_path = None
    if dwg:
        from syntera.drawings.export import export_dwg

        dwg_path = output / "edited.dwg"
        export_dwg(target, dwg_path)
    untouched = [h for h in before if h not in changed]
    return DrawingEditReport(
        description=changes.description,
        source_sha256=hashlib.sha256(data).hexdigest(),
        output_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
        changed=sorted(changed),
        untouched=len(untouched),
        fingerprints_before=before,
        fingerprints_after=after,
        dwg=str(dwg_path) if dwg_path else None,
    )
