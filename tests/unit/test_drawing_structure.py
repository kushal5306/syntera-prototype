"""Richer drawing reading: layouts, attributed blocks, xrefs and MTEXT (D2)."""

from __future__ import annotations

import json
from pathlib import Path

import ezdxf
import pytest

from syntera.drawings.intake import SUMMARY_FILE, ingest_drawing
from syntera.drawings.models import DrawingSummary
from syntera.drawings.reader import read_drawing
from syntera.drawings.structure import block_role


def _attributed_block(doc, name: str, tags: list[str]) -> None:
    block = doc.blocks.new(name)
    block.add_lwpolyline([(0, 0), (100, 0), (100, 10), (0, 10)], close=True)
    for index, tag in enumerate(tags):
        block.add_attdef(tag, (2 + 20 * index, 2), dxfattribs={"height": 2.5})


def structured_dxf(path: Path) -> Path:
    """Model space with BOM rows, a tag and an xref; a paper sheet with a title block."""
    doc = ezdxf.new("R2010", setup=True)
    doc.units = ezdxf.units.MM
    _attributed_block(doc, "TB", ["TITLE", "DWG_NO", "REV", "SCALE"])
    _attributed_block(doc, "BOMROW", ["ITEM", "QTY", "DESCRIPTION"])
    _attributed_block(doc, "TAG", ["TAG"])
    doc.add_xref_def("site.dxf", "SITE")
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (600, 0), (600, 400), (0, 400)], close=True)
    msp.add_mtext("{\\fArial|b1|i0;Pump  P-101}\\PDuty:   standby\\P\\P", dxfattribs={
        "insert": (20, 380), "char_height": 10})  # fmt: skip
    for row, (item, qty, text) in enumerate([("1", "2", "Elbow 90 LR"), ("2", "1", "Tee")]):
        msp.add_blockref("BOMROW", (700, 100 - 12 * row)).add_auto_attribs(
            {"ITEM": item, "QTY": qty, "DESCRIPTION": text}
        )
    msp.add_blockref("TAG", (300, 200)).add_auto_attribs({"TAG": "P-101"})
    msp.add_blockref("SITE", (0, 0))
    sheet = doc.layouts.new("Sheet 1")
    sheet.add_viewport(center=(150, 100), size=(250, 150), view_center_point=(300, 200),
                       view_height=450)  # fmt: skip
    sheet.add_blockref("TB", (200, 10)).add_auto_attribs(
        {"TITLE": "Pump skid GA", "DWG_NO": "SYN-001", "REV": "B", "SCALE": "1:5"}
    )
    doc.saveas(path)
    return path


@pytest.mark.parametrize(
    ("tags", "role"),
    [
        (["TITLE", "DWG_NO", "REV"], "title_block"),
        (["Drawing No.", "Sheet", "Scale", "Item"], "title_block"),
        (["ITEM", "QTY", "DESCRIPTION"], "bom_row"),
        (["Qty", "Part-No"], "bom_row"),
        (["TAG"], "other"),
        (["TITLE", "QTY"], "other"),
    ],
)
def test_block_roles_follow_a_keyword_rule(tags, role):
    assert block_role(tags) == role


def test_reader_lists_layouts_attributed_blocks_xrefs_and_normalised_mtext(tmp_path):
    facts = read_drawing(structured_dxf(tmp_path / "s.dxf"))
    layouts = {layout.name: layout for layout in facts["layouts"]}
    assert "Sheet 1" in layouts
    assert layouts["Sheet 1"].block_inserts == {"TB": 1}
    assert layouts["Sheet 1"].viewports >= 1

    references = facts["block_references"]
    assert [(r.space, r.block, r.role) for r in references] == [
        ("Model", "BOMROW", "bom_row"),
        ("Model", "BOMROW", "bom_row"),
        ("Model", "TAG", "other"),
        ("Sheet 1", "TB", "title_block"),
    ]
    assert references[0].attributes == {"DESCRIPTION": "Elbow 90 LR", "ITEM": "1", "QTY": "2"}
    assert references[3].attributes["DWG_NO"] == "SYN-001"

    (xref,) = facts["xrefs"]
    assert (xref.name, xref.path, xref.overlay, xref.inserts) == ("SITE", "site.dxf", False, 1)

    texts = [note.text for note in facts["texts"]]
    assert "Pump P-101\nDuty: standby" in texts


def test_summary_records_structure_deterministically_and_old_summaries_still_load(tmp_path):
    path = structured_dxf(tmp_path / "s.dxf")
    first = ingest_drawing(path.read_bytes(), "s.dxf", tmp_path / "a")
    second = ingest_drawing(path.read_bytes(), "s.dxf", tmp_path / "b")
    data = (tmp_path / "a" / first.drawing_id / SUMMARY_FILE).read_bytes()
    assert data == (tmp_path / "b" / second.drawing_id / SUMMARY_FILE).read_bytes()
    stored = json.loads(data)
    assert [r["role"] for r in stored["block_references"]].count("bom_row") == 2
    assert stored["xrefs"][0]["name"] == "SITE"

    for key in ("layouts", "block_references", "xrefs"):
        del stored[key]
    old = DrawingSummary.model_validate(stored)
    assert old.layouts == [] and old.block_references == [] and old.xrefs == []
