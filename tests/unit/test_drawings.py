from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import ezdxf
import pytest

from syntera.drawings.convert import DrawingError, available_converters, convert_dwg, dwg_version
from syntera.drawings.intake import SUMMARY_FILE, SVG_FILE, ingest_drawing, source_format


def pump_dxf(path: Path, outlier: bool = False) -> Path:
    """Synthetic 600 x 400 mm pump outline with one dimension and one note."""
    doc = ezdxf.new("R2010", setup=True)
    doc.units = ezdxf.units.MM
    doc.layers.add("OUTLINE", color=7)
    doc.layers.add("PIPE", color=1)
    msp = doc.modelspace()
    msp.add_lwpolyline(
        [(0, 0), (600, 0), (600, 400), (0, 400)], close=True, dxfattribs={"layer": "OUTLINE"}
    )
    msp.add_circle((300, 200), 40, dxfattribs={"layer": "PIPE"})
    msp.add_text("PUMP P-101", height=20, dxfattribs={"layer": "OUTLINE"}).set_placement((20, 420))
    msp.add_linear_dim(base=(0, -50), p1=(0, 0), p2=(600, 0), dxfattribs={"layer": "DIMS"}).render()
    if outlier:
        msp.add_line((0, 0), (5e6, 5e6), dxfattribs={"layer": "STRAY"})
    doc.saveas(path)
    return path


@pytest.fixture
def fake_dwg2dxf(tmp_path, monkeypatch):
    """Install a stand-in ``dwg2dxf`` that writes the DXF named by SYNTERA_FAKE_DXF."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "dwg2dxf"
    script.write_text(
        f"#!{sys.executable}\n"
        "import os, shutil, sys\n"
        "if sys.argv[1] == '--version':\n"
        "    print('dwg2dxf 0.0-test'); sys.exit(0)\n"
        "print('Warning: Unstable Class object MATERIAL', file=sys.stderr)\n"
        "source = os.environ.get('SYNTERA_FAKE_DXF')\n"
        "if source:\n"
        "    shutil.copyfile(source, sys.argv[sys.argv.index('-o') + 1])\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    return script


def test_dwg_version_reads_header_and_rejects_other_files():
    assert dwg_version(b"AC1032\x00\x00") == "AC1032"
    assert dwg_version(b"AC1015rest") == "AC1015"
    with pytest.raises(DrawingError, match="not a DWG"):
        dwg_version(b"PK\x03\x04zip")


def test_source_format_requires_matching_extension_and_content():
    assert source_format("plan.DWG", b"AC1027....") == "dwg"
    assert source_format("plan.dxf", b"  0\nSECTION\n  2\nHEADER\n") == "dxf"
    with pytest.raises(DrawingError, match="only .dwg and .dxf"):
        source_format("plan.pdf", b"%PDF")
    with pytest.raises(DrawingError, match="not a DXF"):
        source_format("plan.dxf", b"hello")
    with pytest.raises(DrawingError, match="not a DWG"):
        source_format("plan.dwg", b"  0\nSECTION")


def test_dxf_summary_reports_units_layers_dimensions_and_notes(tmp_path):
    source = pump_dxf(tmp_path / "pump.dxf")
    summary = ingest_drawing(source.read_bytes(), "pump.dxf", tmp_path / "store")

    assert summary.source_format == "dxf"
    assert summary.conversion is None
    assert summary.units.name == "Millimeters"
    assert summary.units.mm_per_unit == 1.0
    assert summary.entity_types == {
        "CIRCLE": 1,
        "DIMENSION": 1,
        "LWPOLYLINE": 1,
        "TEXT": 1,
    }
    layers = {layer.name: layer.entity_count for layer in summary.layers}
    assert layers["OUTLINE"] == 2
    assert layers["PIPE"] == 1
    assert [(d.kind, d.measurement) for d in summary.dimensions] == [("linear", 600.0)]
    assert [(t.text, t.position) for t in summary.texts] == [("PUMP P-101", (20.0, 420.0))]
    assert summary.clipped_handles == []
    (low_x, low_y), (high_x, high_y) = summary.view_extents
    assert low_x <= 0 and high_x >= 600 and low_y <= -50 and high_y >= 420
    folder = tmp_path / "store" / summary.drawing_id
    assert (folder / SVG_FILE).read_text(encoding="utf-8").startswith("<svg")


def test_ingest_is_deterministic_and_content_addressed(tmp_path):
    data = pump_dxf(tmp_path / "pump.dxf").read_bytes()
    first = ingest_drawing(data, "pump.dxf", tmp_path / "a")
    second = ingest_drawing(data, "renamed.dxf", tmp_path / "b")
    assert first.drawing_id == second.drawing_id
    for name in (SUMMARY_FILE, SVG_FILE):
        a = (tmp_path / "a" / first.drawing_id / name).read_bytes()
        b = (tmp_path / "b" / second.drawing_id / name).read_bytes()
        if name == SUMMARY_FILE:
            a_json, b_json = json.loads(a), json.loads(b)
            assert a_json.pop("filename") == "pump.dxf"
            assert b_json.pop("filename") == "renamed.dxf"
            assert a_json == b_json
        else:
            assert a == b
    assert ingest_drawing(data, "again.dxf", tmp_path / "a").filename == "pump.dxf"


def test_far_away_entity_is_clipped_from_view_but_reported(tmp_path):
    source = pump_dxf(tmp_path / "pump.dxf", outlier=True)
    summary = ingest_drawing(source.read_bytes(), "pump.dxf", tmp_path / "store")
    assert len(summary.clipped_handles) == 1
    assert summary.extents[1][0] == pytest.approx(5e6)
    assert summary.view_extents[1][0] < 1000


def test_empty_model_space_fails_closed_and_leaves_no_record(tmp_path):
    path = tmp_path / "empty.dxf"
    ezdxf.new().saveas(path)
    with pytest.raises(DrawingError, match="no drawable geometry"):
        ingest_drawing(path.read_bytes(), "empty.dxf", tmp_path / "store")
    assert list((tmp_path / "store").iterdir()) == []


def test_dwg_without_converter_explains_how_to_proceed(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert available_converters() == []
    with pytest.raises(DrawingError, match="no DWG converter is installed"):
        ingest_drawing(b"AC1032" + bytes(64), "plan.dwg", tmp_path / "store")


def test_dwg_is_converted_and_conversion_is_recorded(tmp_path, monkeypatch, fake_dwg2dxf):
    monkeypatch.setenv("SYNTERA_FAKE_DXF", str(pump_dxf(tmp_path / "pump.dxf")))
    summary = ingest_drawing(b"AC1032" + bytes(64), "plan.dwg", tmp_path / "store")
    assert summary.source_format == "dwg"
    assert summary.dwg_version == "AC1032 (AutoCAD 2018)"
    assert summary.conversion.converter == "libredwg"
    assert summary.conversion.converter_version == "dwg2dxf 0.0-test"
    assert summary.conversion.warnings == ["Warning: Unstable Class object MATERIAL"]
    assert summary.dimensions[0].measurement == 600.0


def test_converter_without_output_is_a_clear_failure(tmp_path, monkeypatch, fake_dwg2dxf):
    monkeypatch.delenv("SYNTERA_FAKE_DXF", raising=False)
    source = tmp_path / "plan.dwg"
    source.write_bytes(b"AC1032" + bytes(64))
    with pytest.raises(DrawingError, match="libredwg could not convert"):
        convert_dwg(source, tmp_path / "out.dxf")


@pytest.mark.skipif(
    not os.environ.get("SYNTERA_SAMPLE_DWG"), reason="set SYNTERA_SAMPLE_DWG to a real DWG file"
)
def test_real_dwg_converts_with_installed_converter(tmp_path):
    source = Path(os.environ["SYNTERA_SAMPLE_DWG"])
    summary = ingest_drawing(source.read_bytes(), source.name, tmp_path)
    assert summary.conversion is not None
    assert summary.entity_count > 0
