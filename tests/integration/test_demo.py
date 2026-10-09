from __future__ import annotations

import json
from pathlib import Path

import pytest

from syntera.analysis.calculix import DECK_FORMAT_VERSION
from syntera.cli import run_demo
from syntera.drawings.intake import ingest_drawing


def test_demo_generates_assured_outputs(tmp_path):
    project_root = Path(__file__).parents[2]
    output = tmp_path / "demo"
    assert run_demo(project_root / "examples" / "demo_skid.yaml", output) == 0
    expected = {
        "assembly.step",
        "routed_assembly.step",
        "route.json",
        "assurance_report.json",
        "route_preview.png",
        "metrics.json",
        "layout.dxf",
        "fea",
    }
    assert expected == {path.name for path in output.iterdir()}
    report = json.loads((output / "assurance_report.json").read_text(encoding="utf-8"))
    assert report["overall_pass"] is True
    assert report["failure_reasons"] == []

    metrics = json.loads((output / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["piping"]["main_length_mm"] == pytest.approx(report["route_length_mm"], abs=1e-3)
    assert metrics["piping"]["bends"] == report["number_of_bends"]
    workspace = [600, 400, 300]
    assert all(low >= 0 for low in metrics["envelope_min_mm"])
    assert all(
        high <= limit for high, limit in zip(metrics["envelope_max_mm"], workspace, strict=True)
    )
    assert metrics["envelope_volume_m3"] < metrics["frame_volume_m3"]

    manifest = json.loads((output / "fea" / "fea_manifest.json").read_text(encoding="utf-8"))
    assert manifest["generated"] is True
    assert manifest["deck_format_version"] == DECK_FORMAT_VERSION
    assert manifest["mesh"]["element_type"] == "S8R"
    cases = [case["name"] for case in manifest["load_cases"]]
    assert cases == ["design_pressure", "self_weight", "thermal_rise"]
    assert {path.name for path in (output / "fea").iterdir()} == {
        "fea_manifest.json",
        "mesh.inp",
        *(f"{case}.inp" for case in cases),
    }

    layout = ingest_drawing(
        (output / "layout.dxf").read_bytes(), "layout.dxf", tmp_path / "drawings"
    )
    assert layout.units.name == "Millimeters"
    assert sorted(dim.measurement for dim in layout.dimensions) == [300, 300, 400, 400, 600, 600]
    notes = {note.text for note in layout.texts}
    assert "ASSURANCE (EXACT GEOMETRY CHECKS): PASS" in notes
    assert (
        f"ROUTE LENGTH {report['route_length_mm']:.1f}   BENDS {report['number_of_bends']}" in notes
    )
