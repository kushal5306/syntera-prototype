"""A demo with fittings places, checks and exports elbows and flanges (B2)."""

from __future__ import annotations

import json
from pathlib import Path

import cadquery as cq
import yaml

from syntera.cli import main
from syntera.components.fittings import FITTINGS_FILE
from syntera.design import DESIGN_FILE, load_design

PROJECT = Path(__file__).parents[2]
CONFIG = PROJECT / "examples" / "demo_skid_fittings.yaml"


def test_routed_assembly_contains_checked_fittings(tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    assert main(["demo", "--config", str(CONFIG), "--output", str(first)]) == 0
    assert main(["demo", "--config", str(CONFIG), "--output", str(second)]) == 0
    assert (first / FITTINGS_FILE).read_bytes() == (second / FITTINGS_FILE).read_bytes()

    report = json.loads((first / FITTINGS_FILE).read_text(encoding="utf-8"))
    kinds = [f["kind"] for f in report["fittings"]]
    route = json.loads((first / "route.json").read_text(encoding="utf-8"))
    assert kinds.count("pipe_bend") == len(route["points"]) - 2
    assert kinds.count("flange") == 2
    assert report["overall_pass"] is True
    assert len(report["checks"]) == len(report["fittings"])
    assert "Synthetic" in report["tables"]

    solids = cq.importers.importStep(str(first / "routed_assembly.step")).vals()
    count = sum(len(value.Solids()) for value in solids)
    pieces = len(report["straight_pieces"])
    assert count >= pieces + len(report["fittings"])
    assert load_design(first / DESIGN_FILE).lines[0].fittings.rating == "PN16"


def test_a_flange_clash_fails_the_assurance(tmp_path):
    data = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    data["obstacles"].append({"type": "box", "name": "bracket", "center": [48, 245, 100],
                              "size": [10, 10, 20]})  # fmt: skip
    config = tmp_path / "clash.yaml"
    config.write_text(yaml.safe_dump(data), encoding="utf-8")
    output = tmp_path / "out"
    assert main(["demo", "--config", str(config), "--output", str(output)]) == 2
    assurance = json.loads((output / "assurance_report.json").read_text(encoding="utf-8"))
    assert assurance["overall_pass"] is False
    assert "fitting main-F1 (flange) intersects an obstacle" in assurance["failure_reasons"]
