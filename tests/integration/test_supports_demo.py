"""Supports and spools are placed with rationale and passed to the FEA decks (B3)."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from syntera.cli import main
from syntera.components.supports import SUPPORTS_FILE

PROJECT = Path(__file__).parents[2]
CONFIG = PROJECT / "examples" / "demo_skid_supports.yaml"


def test_supported_demo_writes_supports_spools_and_fea_restraints(tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    assert main(["demo", "--config", str(CONFIG), "--output", str(first)]) == 0
    assert main(["demo", "--config", str(CONFIG), "--output", str(second)]) == 0
    assert (first / SUPPORTS_FILE).read_bytes() == (second / SUPPORTS_FILE).read_bytes()

    layout = json.loads((first / SUPPORTS_FILE).read_text(encoding="utf-8"))
    supports, spools = layout["supports"], layout["spools"]
    assert supports and len(spools) >= 2
    assert all(spool["length_mm"] <= 400 + 1e-6 for spool in spools)
    marks = [0.0, *[s["distance_mm"] for s in supports], spools[-1]["end_mm"]]
    assert all(b - a <= 250 + 1e-6 for a, b in zip(marks, marks[1:], strict=False))
    rules = {record["path"]: record for record in layout["rationale"]}
    assert rules["supports.maximum_span"]["source"] == "user"
    assert rules["supports.count"]["rule_id"] == "rules.support_placement_max_span.v1"
    assert rules["spools.count"]["rule_id"] == "rules.spool_split_max_length.v1"

    manifest = json.loads((first / "fea" / "fea_manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["supports"]) == len(supports)
    mesh = (first / "fea" / "mesh.inp").read_text(encoding="utf-8")
    deck = (first / "fea" / "design_pressure.inp").read_text(encoding="utf-8")
    for index in range(1, len(supports) + 1):
        assert f"*NSET, NSET=NSUP{index}" in mesh
        assert f"NSUP{index}, 3, 3" in deck
    assert (first / "fea" / "mesh.inp").read_bytes() == (second / "fea" / "mesh.inp").read_bytes()


def test_span_rule_from_wall_and_material_and_unsupported_lines_fail(tmp_path):
    data = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    del data["supports"]["maximum_span_mm"]
    config = tmp_path / "calc.yaml"
    config.write_text(yaml.safe_dump(data), encoding="utf-8")
    output = tmp_path / "calc"
    assert main(["demo", "--config", str(config), "--output", str(output)]) == 0
    layout = json.loads((output / SUPPORTS_FILE).read_text(encoding="utf-8"))
    span = next(r for r in layout["rationale"] if r["path"] == "supports.maximum_span")
    assert span["rule_id"] == "rules.support_span_deflection.v1"
    assert layout["supports"] == []  # the short demo line needs no intermediate support

    data["supports"]["maximum_span_mm"] = 20  # shorter than any bend: cannot be supported
    config.write_text(yaml.safe_dump(data), encoding="utf-8")
    output = tmp_path / "short"
    assert main(["demo", "--config", str(config), "--output", str(output)]) == 2
    report = json.loads((output / "assurance_report.json").read_text(encoding="utf-8"))
    assert any(r.startswith("supports could not be placed") for r in report["failure_reasons"])
    assert not (output / "fea").exists()
