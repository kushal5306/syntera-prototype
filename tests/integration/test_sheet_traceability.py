"""Every entity on a generated sheet resolves to a design item, view or sheet part (C4)."""

from __future__ import annotations

from pathlib import Path

import ezdxf

from syntera.cli import main
from syntera.design import DESIGN_FILE, load_design
from syntera.drawings.sheet import SHEET_DXF
from syntera.drawings.trace import ref_of, resolve, trace_dxf

PROJECT = Path(__file__).parents[2]


def test_every_sheet_entity_resolves_to_the_design(tmp_path):
    run = tmp_path / "demo"
    assert main(["demo", "--config", str(PROJECT / "examples" / "demo_skid.yaml"),
                 "--output", str(run)]) == 0  # fmt: skip
    assert main(["sheet", "--run", str(run), "--output", str(tmp_path / "sheet")]) == 0
    design = load_design(run / DESIGN_FILE)
    path = tmp_path / "sheet" / SHEET_DXF

    report = trace_dxf(path, design)
    assert report.untagged == []
    assert report.unresolved == []
    assert report.resolved == report.entities > 0
    assert set(report.by_kind) == {"equipment", "nozzle", "line", "view", "drawing"}

    refs = {ref_of(entity) for entity in ezdxf.readfile(path).modelspace()}
    for item in design.equipment:
        assert f"equipment:{item.id}" in refs
    assert "line:main" in refs
    assert {"nozzle:start", "nozzle:end"} <= refs
    # a selected dimension resolves to the line it measures
    dims = [e for e in ezdxf.readfile(path).modelspace().query("DIMENSION")]
    kinds = {resolve(ref_of(dim), design).kind for dim in dims}
    assert kinds == {"view", "nozzle", "line"}
