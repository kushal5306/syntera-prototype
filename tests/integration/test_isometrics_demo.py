"""The BOM of a routed demo matches the fittings the pipeline placed and checked (C3)."""

from __future__ import annotations

import json
from pathlib import Path

from syntera.cli import main
from syntera.components.fittings import FITTINGS_FILE
from syntera.drawings.isometric import BOM_FILE

PROJECT = Path(__file__).parents[2]


def test_isometrics_cli_bom_matches_pipeline_fittings(tmp_path):
    run = tmp_path / "demo"
    config = PROJECT / "examples" / "demo_skid_fittings.yaml"
    assert main(["demo", "--config", str(config), "--output", str(run)]) == 0
    first, second = tmp_path / "a", tmp_path / "b"
    assert main(["isometrics", "--run", str(run), "--output", str(first)]) == 0
    assert main(["isometrics", "--run", str(run), "--output", str(second)]) == 0
    assert (first / BOM_FILE).read_bytes() == (second / BOM_FILE).read_bytes()
    assert (first / "iso_main.dxf").is_file()

    bom = json.loads((first / BOM_FILE).read_text(encoding="utf-8"))
    placed = json.loads((run / FITTINGS_FILE).read_text(encoding="utf-8"))
    (takeoff,) = bom["lines"]
    rows = {row["item"]: row for row in takeoff["rows"]}
    kinds = [fitting["kind"] for fitting in placed["fittings"]]
    assert rows["pipe_bend"]["quantity"] == kinds.count("pipe_bend")
    assert rows["flange"]["quantity"] == kinds.count("flange") == 2
    assert rows["pipe"]["lengths_mm"] == [p["length_mm"] for p in placed["straight_pieces"]]
    assert rows["pipe"]["material"] == "stainless_316"


def test_isometrics_need_a_routed_design(tmp_path, capsys):
    design = PROJECT / "examples" / "demo_three_lines.design.json"
    assert main(["isometrics", "--run", str(design), "--output", str(tmp_path)]) == 2
    assert "no routed lines" in capsys.readouterr().err
