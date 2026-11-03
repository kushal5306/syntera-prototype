"""Round-trip DXF editing keeps untouched entities byte-stable (D4)."""

from __future__ import annotations

import json
from pathlib import Path

import ezdxf
import pytest

from syntera.cli import main
from syntera.drawings.convert import DrawingError
from syntera.drawings.edit import (
    EDIT_REPORT,
    EDITED_DXF,
    DrawingChangeSet,
    edit_dxf,
    entity_fingerprint,
)


def plant(path: Path) -> dict[str, str]:
    doc = ezdxf.new("R2018", setup=True)
    block = doc.blocks.new("TAGBOX")
    block.add_attdef("TAG", (0, 0), dxfattribs={"height": 2.5})
    msp = doc.modelspace()
    handles = {
        "pump": msp.add_lwpolyline([(0, 0), (600, 0), (600, 400), (0, 400)], close=True,
                                   dxfattribs={"layer": "EQUIPMENT"}).dxf.handle,
        "pipe": msp.add_line((600, 200), (900, 200), dxfattribs={"layer": "PIPE"}).dxf.handle,
        "nozzle": msp.add_circle((300, 200), 40).dxf.handle,
        "label": msp.add_text("PUMP P-101", height=20).dxf.handle,
        "note": msp.add_mtext("Feed to P-101\\Pnot P-1010").dxf.handle,
        "other": msp.add_text("VALVE V-201", height=10).dxf.handle,
        "far": msp.add_arc((2000, 2000), 50, 0, 90).dxf.handle,
    }  # fmt: skip
    insert = msp.add_blockref("TAGBOX", (650, 220))
    insert.add_auto_attribs({"TAG": "P-101"})
    handles["tag"] = insert.dxf.handle
    msp.add_linear_dim(base=(0, -50), p1=(0, 0), p2=(600, 0)).render()
    doc.saveas(path)
    return handles


def _changes(h: dict[str, str]) -> DrawingChangeSet:
    return DrawingChangeSet.model_validate(
        {
            "description": "move the pipe, relayer the nozzle, rename P-101",
            "edits": [
                {"op": "move", "handle": h["pipe"], "offset": [0, 50, 0]},
                {"op": "set_layer", "handle": h["nozzle"], "layer": "NOZZLE"},
                {"op": "retag", "old": "P-101", "new": "P-201"},
                {"op": "set_text", "handle": h["other"], "text": "VALVE V-202"},
            ],
        }
    )


def test_only_named_entities_change_and_the_rest_are_byte_stable(tmp_path):
    source = tmp_path / "plant.dxf"
    h = plant(source)
    report = edit_dxf(source, _changes(h), tmp_path / "out")
    expected = {h["pipe"], h["nozzle"], h["label"], h["note"], h["tag"], h["other"]}
    assert set(report.changed) == expected
    untouched = [handle for handle in report.fingerprints_before if handle not in expected]
    assert untouched and report.untouched == len(untouched)
    for handle in untouched:
        assert report.fingerprints_after[handle] == report.fingerprints_before[handle], handle
    for handle in expected:
        assert report.fingerprints_after[handle] != report.fingerprints_before[handle], handle

    doc = ezdxf.readfile(tmp_path / "out" / EDITED_DXF)
    get = doc.entitydb.get
    assert tuple(get(h["pipe"]).dxf.start) == (600, 250, 0)
    assert get(h["nozzle"]).dxf.layer == "NOZZLE"
    assert get(h["label"]).dxf.text == "PUMP P-201"
    assert get(h["note"]).text == "Feed to P-201\\Pnot P-1010"  # whole tags only
    assert get(h["tag"]).attribs[0].dxf.text == "P-201"
    assert get(h["other"]).dxf.text == "VALVE V-202"


def test_fingerprints_are_stable_across_a_plain_save(tmp_path):
    source = tmp_path / "plant.dxf"
    plant(source)
    doc = ezdxf.readfile(source)
    doc.saveas(tmp_path / "again.dxf")
    again = ezdxf.readfile(tmp_path / "again.dxf")
    for entity in doc.modelspace():
        other = again.entitydb.get(entity.dxf.handle)
        assert entity_fingerprint(entity, doc.dxfversion) == entity_fingerprint(
            other, again.dxfversion
        )


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        ({"op": "move", "handle": "FFFF", "offset": [1, 0, 0]}, "no entity with handle FFFF"),
        ({"op": "set_text", "handle": "PIPE", "text": "x"}, "not a text"),
        ({"op": "retag", "old": "X-999", "new": "X-1"}, "does not appear"),
    ],
)
def test_bad_edits_are_refused_with_the_edit_number(tmp_path, edit, message):
    source = tmp_path / "plant.dxf"
    h = plant(source)
    if edit.get("handle") == "PIPE":
        edit = {**edit, "handle": h["pipe"]}
    changes = DrawingChangeSet.model_validate({"description": "bad", "edits": [edit]})
    with pytest.raises(DrawingError, match=rf"edit 1 \({edit['op']}\).*{message}"):
        edit_dxf(source, changes, tmp_path / "out")


def test_cli_writes_report_and_dwg_needs_a_converter(tmp_path, monkeypatch, capsys):
    source = tmp_path / "plant.dxf"
    h = plant(source)
    changes = tmp_path / "changes.json"
    changes.write_text(_changes(h).model_dump_json(), encoding="utf-8")
    out = tmp_path / "out"
    assert main(["drawing-edit", "--input", str(source), "--changes", str(changes),
                 "--output", str(out)]) == 0  # fmt: skip
    report = json.loads((out / EDIT_REPORT).read_text(encoding="utf-8"))
    assert len(report["changed"]) == 6
    assert "byte-stable: " in capsys.readouterr().out
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert main(["drawing-edit", "--input", str(source), "--changes", str(changes),
                 "--output", str(tmp_path / "dwg"), "--dwg"]) == 2  # fmt: skip
    assert "no DWG writer is installed" in capsys.readouterr().err
