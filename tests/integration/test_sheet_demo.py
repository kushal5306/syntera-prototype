"""A demo run becomes an ISO sheet that audits clean and repeats the verdict (C2)."""

from __future__ import annotations

from pathlib import Path

import ezdxf

from syntera.cli import main
from syntera.drawings.sheet import SHEET_DXF, SHEET_FILE, SheetLayout

PROJECT = Path(__file__).parents[2]


def test_demo_run_sheet_is_deterministic_and_audits_clean(tmp_path, capsys):
    run = tmp_path / "demo"
    assert main(["demo", "--config", str(PROJECT / "examples" / "demo_skid.yaml"),
                 "--output", str(run)]) == 0  # fmt: skip
    first, second = tmp_path / "a", tmp_path / "b"
    assert main(["sheet", "--run", str(run), "--output", str(first)]) == 0
    assert main(["sheet", "--run", str(run), "--output", str(second)]) == 0
    assert "Title block assurance (repeated from the report): PASS" in capsys.readouterr().out
    assert (first / SHEET_FILE).read_bytes() == (second / SHEET_FILE).read_bytes()

    layout = SheetLayout.model_validate_json((first / SHEET_FILE).read_bytes())
    assert layout.choice.preferred
    kinds = {dim.kind for dim in layout.dimensions}
    assert kinds == {"overall", "nozzle", "run"}
    assert layout.drawing_number.startswith("SYN-")

    doc = ezdxf.readfile(first / SHEET_DXF)
    auditor = doc.audit()
    assert not auditor.has_errors, [str(error) for error in auditor.errors]
    assert len(doc.modelspace().query("DIMENSION")) == len(layout.dimensions)


def test_sheet_needs_a_routed_run(tmp_path, capsys):
    assert main(["design", "--config", str(PROJECT / "examples" / "demo_skid.yaml"),
                 "--output", str(tmp_path)]) == 0  # fmt: skip
    assert main(["sheet", "--run", str(tmp_path), "--output", str(tmp_path / "s")]) == 2
    assert "no routed_assembly.step" in capsys.readouterr().err
