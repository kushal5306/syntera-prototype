"""Drawing structure beyond one space: layouts, attributed blocks, xrefs and MTEXT (D2).

Everything here is read deterministically from the DXF: paper-space layouts in tab order,
every block insert that carries attributes (with a keyword rule naming title blocks and
BOM rows), external references, and MTEXT normalised to plain lines.
"""

from __future__ import annotations

import re
from collections import Counter

from ezdxf.document import Drawing
from ezdxf.entities import Insert, MText

from syntera.drawings.models import BlockReference, BlockRole, LayoutSummary, XrefSummary

MAX_BLOCK_REFERENCES = 500
# Attribute tags (upper case, separators removed) that mark a title block or a BOM row.
TITLE_TAGS = frozenset(
    {"TITLE", "DWGNO", "DRAWINGNO", "DRAWINGNUMBER", "SHEET", "SCALE", "REV", "REVISION",
     "DRAWNBY", "CHECKEDBY", "APPROVEDBY", "DATE", "PROJECT"}
)  # fmt: skip
BOM_TAGS = frozenset(
    {"ITEM", "ITEMNO", "QTY", "QUANTITY", "PARTNO", "PARTNUMBER", "DESCRIPTION", "MATERIAL"}
)
_SEPARATORS = re.compile(r"[^A-Z0-9]")


def _tag_key(tag: str) -> str:
    return _SEPARATORS.sub("", tag.upper())


def block_role(tags: list[str]) -> BlockRole:
    """``title_block`` with three or more title tags, ``bom_row`` with a quantity or item tag
    and a description or part tag; otherwise ``other``. Title wins a tie."""
    keys = {_tag_key(tag) for tag in tags}
    if len(keys & TITLE_TAGS) >= 3:
        return "title_block"
    if keys & {"ITEM", "ITEMNO", "QTY", "QUANTITY"} and keys & {
        "DESCRIPTION", "PARTNO", "PARTNUMBER", "MATERIAL"
    }:  # fmt: skip
        return "bom_row"
    return "other"


def normalise_mtext(entity: MText) -> str:
    """MTEXT as plain text: formatting codes removed, lines stripped, blank runs dropped."""
    lines = [line.strip() for line in entity.plain_text(split=True)]
    kept: list[str] = []
    for line in lines:
        if line or (kept and kept[-1]):
            kept.append(line)
    while kept and not kept[-1]:
        kept.pop()
    return "\n".join(re.sub(r"[ \t]+", " ", line) for line in kept)


def _spaces(doc: Drawing) -> list[tuple[str, object]]:
    spaces: list[tuple[str, object]] = [("Model", doc.modelspace())]
    for name in doc.layouts.names_in_taborder():
        if name != "Model":
            spaces.append((name, doc.layouts.get(name)))
    return spaces


def layout_summaries(doc: Drawing) -> list[LayoutSummary]:
    summaries = []
    for order, (name, space) in enumerate(_spaces(doc)):
        if name == "Model":
            continue
        entities = list(space)  # type: ignore[call-overload]
        inserts = Counter(e.dxf.name for e in entities if e.dxftype() == "INSERT")
        summaries.append(
            LayoutSummary(
                name=name,
                tab_order=order,
                entity_count=len(entities),
                # includes the overall paper viewport when the file has one
                viewports=sum(1 for e in entities if e.dxftype() == "VIEWPORT"),
                block_inserts=dict(sorted(inserts.items())),
            )
        )
    return summaries


def block_references(doc: Drawing) -> tuple[list[BlockReference], bool]:
    """Attributed block inserts in every space, in drawing order; capped."""
    found: list[BlockReference] = []
    truncated = False
    for space_name, space in _spaces(doc):
        for entity in space:  # type: ignore[attr-defined]
            if not isinstance(entity, Insert) or not entity.attribs:
                continue
            if len(found) >= MAX_BLOCK_REFERENCES:
                truncated = True
                break
            attributes = {attrib.dxf.tag: attrib.dxf.text.strip() for attrib in entity.attribs}
            point = entity.dxf.insert
            found.append(
                BlockReference(
                    handle=entity.dxf.handle,
                    space=space_name,
                    block=entity.dxf.name,
                    layer=entity.dxf.layer,
                    position=(round(point.x, 6), round(point.y, 6)),
                    role=block_role(list(attributes)),
                    attributes=dict(sorted(attributes.items())),
                )
            )
    return found, truncated


def xref_summaries(doc: Drawing) -> list[XrefSummary]:
    inserts: Counter[str] = Counter()
    for _, space in _spaces(doc):
        for entity in space.query("INSERT"):  # type: ignore[attr-defined]
            inserts[entity.dxf.name] += 1
    xrefs = []
    for block in doc.blocks:
        record = block.block
        if record is None or not record.is_xref:
            continue
        xrefs.append(
            XrefSummary(
                name=block.name,
                path=str(record.dxf.get("xref_path", "")),
                overlay=bool(record.is_xref_overlay),
                inserts=inserts.get(block.name, 0),
            )
        )
    return sorted(xrefs, key=lambda item: item.name.lower())
