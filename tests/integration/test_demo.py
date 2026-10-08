from __future__ import annotations

import json
from pathlib import Path

from syntera.analysis.calculix import DECK_FORMAT_VERSION
from syntera.cli import run_demo


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
        "fea",
    }
    assert expected == {path.name for path in output.iterdir()}
    report = json.loads((output / "assurance_report.json").read_text(encoding="utf-8"))
    assert report["overall_pass"] is True
    assert report["failure_reasons"] == []

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
