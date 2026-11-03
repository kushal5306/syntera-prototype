"""DXF to DWG export through an external ODA File Converter (D1).

A stand-in converter on PATH exercises staging, validation and provenance; the real ODA
File Converter is used only when it is installed, and that test skips cleanly otherwise.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import stat
import sys
from pathlib import Path

import ezdxf
import pytest

from syntera.cli import main
from syntera.drawings.convert import DrawingError
from syntera.drawings.export import DWG_TARGETS, ODA_EXECUTABLE, dwg_writer, export_dwg

FAKE_ODA = """#!{python}
import os, sys
from pathlib import Path
CODES = {codes!r}
inbox, outbox, version, kind = sys.argv[1:5]
assert kind == "DWG"
mode = os.environ.get("FAKE_ODA_MODE", "ok")
if mode == "fail":
    print("ERROR: cannot read input", file=sys.stderr)
    sys.exit(1)
for source in sorted(Path(inbox).glob("*.dxf")):
    code = "AC1015" if mode == "wrong" else CODES[version]
    body = code.encode() + b"\\0" + source.read_bytes()
    (Path(outbox) / (source.stem + ".dwg")).write_bytes(body)
print("Converted 1 file")
"""


@pytest.fixture
def fake_oda(tmp_path, monkeypatch):
    folder = tmp_path / "bin"
    folder.mkdir()
    script = folder / ODA_EXECUTABLE
    script.write_text(FAKE_ODA.format(python=sys.executable, codes=DWG_TARGETS), encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(folder))
    return script


@pytest.fixture
def dxf(tmp_path):
    doc = ezdxf.new("R2018", setup=True)
    doc.modelspace().add_line((0, 0), (100, 0))
    path = tmp_path / "layout.dxf"
    doc.saveas(path)
    return path


def test_no_writer_is_reported_clearly(dxf, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert dwg_writer() is None
    with pytest.raises(DrawingError, match="no DWG writer is installed"):
        export_dwg(dxf, tmp_path / "out.dwg")
    assert main(["export-dwg", "--input", str(dxf), "--output", str(tmp_path / "x.dwg")]) == 2
    assert "ODA File Converter" in capsys.readouterr().err
    assert not (tmp_path / "x.dwg").exists()


def test_export_records_converter_release_and_hashes(fake_oda, dxf, tmp_path):
    target = tmp_path / "out" / "layout.dwg"
    record = export_dwg(dxf, target, "ACAD2013")
    data = target.read_bytes()
    assert data.startswith(b"AC1027")
    assert record.converter == "oda" and record.converter_path == str(fake_oda)
    assert (record.target, record.release_code, record.release) == (
        "ACAD2013", "AC1027", "AutoCAD 2013",
    )  # fmt: skip
    assert record.source_sha256 == hashlib.sha256(dxf.read_bytes()).hexdigest()
    assert record.sha256 == hashlib.sha256(data).hexdigest()


def test_export_cli_writes_dwg_and_record(fake_oda, dxf, tmp_path):
    target = tmp_path / "layout.dwg"
    assert main(["export-dwg", "--input", str(dxf), "--output", str(target)]) == 0
    record = json.loads(target.with_suffix(".export.json").read_text(encoding="utf-8"))
    assert record["release"] == "AutoCAD 2018"
    assert target.read_bytes().startswith(b"AC1032")


@pytest.mark.parametrize(
    ("mode", "message"), [("fail", "cannot read input"), ("wrong", "AutoCAD 2000 DWG")]
)
def test_failed_or_wrong_conversions_are_refused(fake_oda, dxf, tmp_path, monkeypatch, mode,
                                                 message):  # fmt: skip
    monkeypatch.setenv("FAKE_ODA_MODE", mode)
    target = tmp_path / "layout.dwg"
    with pytest.raises(DrawingError, match=message):
        export_dwg(dxf, target)
    assert not target.exists()


def test_bad_inputs_are_refused(fake_oda, dxf, tmp_path):
    with pytest.raises(DrawingError, match="unknown DWG release"):
        export_dwg(dxf, tmp_path / "a.dwg", "ACAD1999")
    with pytest.raises(DrawingError, match="a DXF file is required"):
        export_dwg(tmp_path / "missing.dxf", tmp_path / "a.dwg")


@pytest.mark.skipif(shutil.which(ODA_EXECUTABLE) is None, reason="ODA File Converter not installed")
def test_real_oda_converter_writes_a_dwg_that_reads_back(dxf, tmp_path):
    from syntera.drawings.convert import convert_dwg

    target = tmp_path / "layout.dwg"
    record = export_dwg(dxf, target)
    assert record.release_code == "AC1032"
    convert_dwg(target, tmp_path / "back.dxf")
    back = ezdxf.readfile(tmp_path / "back.dxf")
    assert len(back.modelspace().query("LINE")) == 1


def test_web_capabilities_report_dwg_export(fake_oda, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from syntera.web.app import create_app

    project = Path(__file__).parents[2]
    client = TestClient(create_app(project / "examples" / "demo_skid.yaml", tmp_path / "web"))
    assert client.get("/api/drawings/capabilities").json()["dwg_export"] is True
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert client.get("/api/drawings/capabilities").json()["dwg_export"] is False
