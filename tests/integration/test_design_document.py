"""A design document converted from YAML drives the same run as the YAML itself."""

from __future__ import annotations

from pathlib import Path

from syntera.cli import main
from syntera.config import load_sized_config
from syntera.design import DESIGN_FILE, load_design, run_config, run_rationale
from syntera.pipeline import execute_pipeline
from syntera.rationale import RATIONALE_FILE

PROJECT = Path(__file__).parents[2]
SIZED = PROJECT / "examples" / "demo_skid_sized.yaml"


def test_design_cli_writes_byte_identical_documents(tmp_path, capsys):
    assert main(["design", "--config", str(SIZED), "--output", str(tmp_path / "a")]) == 0
    assert main(["design", "--config", str(SIZED), "--output", str(tmp_path / "b")]) == 0
    first = (tmp_path / "a" / DESIGN_FILE).read_bytes()
    assert first == (tmp_path / "b" / DESIGN_FILE).read_bytes()
    out = capsys.readouterr().out
    assert "schema version 1" in out
    assert "lines: main" in out
    assert "overall_pass" not in first.decode("utf-8")


def test_run_from_design_matches_run_from_yaml(tmp_path):
    config, records = load_sized_config(SIZED)
    from_yaml = execute_pipeline(config, tmp_path / "yaml", create_preview=False, rationale=records)

    assert main(["design", "--config", str(SIZED), "--output", str(tmp_path / "design")]) == 0
    design = load_design(tmp_path / "design" / DESIGN_FILE)
    from_design = execute_pipeline(
        run_config(design),
        tmp_path / "from-design",
        create_preview=False,
        rationale=run_rationale(design),
    )

    for name in ("route.json", RATIONALE_FILE):
        assert (tmp_path / "yaml" / name).read_bytes() == (
            tmp_path / "from-design" / name
        ).read_bytes(), name
    assert from_design.report.overall_pass == from_yaml.report.overall_pass is True
