from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from syntera.cli import main, run_demo

PROJECT_ROOT = Path(__file__).parents[2]


def test_compare_demo_against_synthetic_baseline(tmp_path):
    output = tmp_path / "demo"
    assert run_demo(PROJECT_ROOT / "examples" / "demo_skid.yaml", output) == 0
    baseline = PROJECT_ROOT / "examples" / "baseline_skid.yaml"
    args = ["compare", "--baseline", str(baseline), "--output", str(output)]

    if shutil.which("ccx") is None:
        # Without solved FEA the comparison is written but never counted.
        assert main(args) == 2
        report = json.loads((output / "comparison.json").read_text(encoding="utf-8"))
        assert report["design_valid"] is False
        assert report["kpi2_met"] is False
        return

    for deck in ("design_pressure", "self_weight", "thermal_rise"):
        subprocess.run(["ccx", "-i", deck], cwd=output / "fea", check=True, capture_output=True)
    config = str(PROJECT_ROOT / "examples" / "demo_skid.yaml")
    assert main(["fea-evaluate", "--config", config, "--output", str(output)]) == 0
    assert main(args) == 0
    report = json.loads((output / "comparison.json").read_text(encoding="utf-8"))
    metrics = json.loads((output / "metrics.json").read_text(encoding="utf-8"))
    assert report["design_valid"] is True
    assert report["validity_reasons"] == []
    assert report["design"]["package_volume_m3"] == metrics["envelope_volume_m3"]
    assert report["design"]["main_piping_length_mm"] == metrics["piping"]["main_length_mm"]
    assert 0 < report["package_volume_ratio"] < 1
    assert report["kpi2_met"] is True
    assert report["baseline_synthetic"] is True
    assert len(report["baseline_sha256"]) == 64
