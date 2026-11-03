"""The routed demo assembly projects deterministically into hidden-line views (C1)."""

from __future__ import annotations

from pathlib import Path

import ezdxf

from syntera.cli import main
from syntera.config import load_sized_config
from syntera.drawings.projection import PROJECTION_FILE, VIEWS_DXF, Projection
from syntera.pipeline import execute_pipeline

PROJECT = Path(__file__).parents[2]


def test_demo_assembly_projection_is_deterministic(tmp_path):
    config, records = load_sized_config(PROJECT / "examples" / "demo_skid.yaml")
    result = execute_pipeline(config, tmp_path / "demo", create_preview=False, rationale=records)
    assert result.report.overall_pass
    step = tmp_path / "demo" / "routed_assembly.step"

    first, second = tmp_path / "a", tmp_path / "b"
    assert main(["project", "--step", str(step), "--output", str(first)]) == 0
    assert main(["project", "--step", str(step), "--output", str(second)]) == 0
    data = (first / PROJECTION_FILE).read_bytes()
    assert data == (second / PROJECTION_FILE).read_bytes()

    projection = Projection.model_validate_json(data)
    assert len(projection.sources) >= 3  # equipment, ports and the routed tube
    plan = projection.view("plan")
    (x0, y0), (x1, y1) = plan.bounds
    assert x0 >= -1e-3 and y0 >= -1e-3 and x1 <= 600 + 1e-3 and y1 <= 400 + 1e-3
    for view in projection.views:
        assert view.count(True) > 0, view.name
    assert sum(view.count(False) for view in projection.views) > 0
    assert any(p.edge in {"smooth", "outline"} for p in plan.polylines)

    doc = ezdxf.readfile(first / VIEWS_DXF)
    assert not doc.audit().has_errors
