from __future__ import annotations

import math
from pathlib import Path

import ezdxf
import pytest

from syntera.config import load_config
from syntera.drawings.intake import DXF_FILE, ingest_drawing
from syntera.drawings.layout3d import corner_points, infer_layout, layer_role
from syntera.drawings.models import AssemblyOptions
from syntera.drawings.piping_check import (
    ASSEMBLY_DIR,
    REPORT_FILE,
    bend_space_problems,
    check_drawing,
    voxel_resolution,
)
from syntera.drawings.reader import drawing_units, load_dxf
from syntera.pipeline import execute_pipeline

PROJECT = Path(__file__).parents[2]


def _infer(path: Path, options: AssemblyOptions | None = None):
    doc, _, _ = load_dxf(path)
    return infer_layout(doc, drawing_units(doc).mm_per_unit, options or AssemblyOptions())


def _check(path: Path, root: Path, options: AssemblyOptions | None = None):
    summary = ingest_drawing(path.read_bytes(), path.name, root)
    doc, _, _ = load_dxf(root / summary.drawing_id / DXF_FILE)
    return check_drawing(doc, summary, root / summary.drawing_id, options or AssemblyOptions())


def test_layer_roles_follow_names_and_overrides():
    assert layer_role("PIPE-CL", {}) == "pipe_centreline"
    assert layer_role("P-Piping", {}) == "pipe"
    assert layer_role("EQUIP-VESSELS", {}) == "equipment"
    assert layer_role("NOZZLES", {}) == "nozzle"
    assert layer_role("M-EQPM", {}) == "equipment"  # US National CAD Standard
    assert layer_role("M-EQPM-NOZL", {}) == "nozzle"
    assert layer_role("A-WALL", {}) is None
    assert layer_role("A-WALL", {"A-WALL": "equipment"}) == "equipment"


def test_corner_points_recover_corners_from_sampled_bends():
    radius = 40.0
    arc = [
        (100 - radius + radius * math.sin(t), radius - radius * math.cos(t))
        for t in (math.pi / 2 * k / 12 for k in range(13))
    ]
    run = [(0.0, 0.0), *arc, (100.0, 200.0)]
    corners = corner_points(run, radius * 0.3, 0.5)
    assert corners == [(0.0, 0.0), (100.0, 0.0), (100.0, 200.0)]


def test_bend_space_problems_need_a_bend_radius_per_bend():
    points = [(0.0, 0.0, 0.0), (100.0, 0.0, 0.0), (100.0, 60.0, 0.0), (300.0, 60.0, 0.0)]
    assert bend_space_problems(points, 20.0) == []
    problems = bend_space_problems(points, 40.0)
    assert problems == ["leg 2 is 60 mm long but its bends need 80 mm"]


def test_voxel_resolution_keeps_the_grid_searchable():
    assert voxel_resolution((600.0, 400.0, 300.0)) == 20.0
    assert voxel_resolution((6000.0, 3000.0, 1200.0)) == 100.0


def test_syntera_layout_dxf_rebuilds_the_configuration_it_was_drawn_from(tmp_path):
    config = load_config(PROJECT / "examples" / "demo_skid.yaml")
    result = execute_pipeline(config, tmp_path / "demo", create_preview=False)
    layout = _infer(tmp_path / "demo" / "layout.dxf")

    assert layout.supported
    assert layout.views == ["plan", "front", "side"]
    assert layout.workspace == config.workspace.dimensions
    assert layout.tube == config.tube
    # The drawing labels equipment in capitals; everything else round-trips exactly.
    assert [o.model_copy(update={"name": o.name.lower()}) for o in layout.obstacles] == list(
        config.obstacles
    )
    assert len(layout.lines) == 1
    assert layout.lines[0].points == [tuple(p) for p in result.route.points]
    assert all(a.source == "drawing" for a in layout.assumptions)


def test_plan_only_layout_uses_stated_defaults(plan_layout_dxf):
    layout = _infer(plan_layout_dxf)

    assert layout.supported
    assert layout.views == []
    assert layout.workspace == (6000.0, 3000.0, 1120.0)
    names = sorted(o.name for o in layout.obstacles)
    assert names == ["BAG FILTER", "CONTROL PANEL", "FEED TANK", "TRANSFER PUMP"]
    sources = {a.quantity: a.source for a in layout.assumptions}
    assert sources["Equipment height"] == "default"
    assert sources["Pipe centreline elevation"] == "default"
    assert sources["Pipe outside diameter"] == "drawing"  # from the nozzle circles
    assert layout.lines[0].points[0] == (300.0, 600.0, 500.0)
    assert layout.lines[0].orthogonal


def test_piping_check_finds_the_clash_and_a_verified_shorter_route(plan_layout_dxf, tmp_path):
    result = _check(plan_layout_dxf, tmp_path)
    report = result.report
    line = report.lines[0]

    assert line.drawn.buildable and not line.drawn.assurance_pass
    assert line.drawn.too_close_to == ["BAG FILTER"]
    assert line.optimised.assurance_pass
    assert line.length_saving_mm == pytest.approx(3800.0)
    assert line.cost_saving == pytest.approx(456.0)
    assert line.nozzle_shift_mm == 0.0
    assert "clashes with equipment or the skid limits (BAG FILTER)" in report.findings[0]
    assert "3800 mm (40.2%) shorter" in report.findings[1]
    folder = tmp_path / report.drawing_id / ASSEMBLY_DIR
    assert (folder / REPORT_FILE).is_file()
    for name in ("drawn_assembly.step", "routed_assembly.step", "config.json", "layout.dxf"):
        assert (folder / "line-1" / name).is_file()


def test_user_heights_change_the_assembly_and_the_answer(plan_layout_dxf, tmp_path):
    report = _check(plan_layout_dxf, tmp_path, AssemblyOptions(equipment_height_mm=400)).report
    line = report.lines[0]

    assert line.drawn.assurance_pass  # the pipe now passes over the filter
    assert line.optimised.bends == 0  # and straight over everything
    assert line.length_saving_mm == pytest.approx(9445.487 - 5400.0)
    heights = [a for a in report.assumptions if a.quantity == "Equipment height"]
    assert heights[0].source == "user"


def test_schematic_drawing_is_refused_with_a_clear_message(tmp_path):
    doc = ezdxf.new("R2018")
    msp = doc.modelspace()
    msp.add_line((0, 0), (100, 0), dxfattribs={"layer": "SVG_000000"})
    msp.add_text("P&ID UNIT 100", dxfattribs={"layer": "SVG_000000"})
    path = tmp_path / "pid.dxf"
    doc.saveas(path)

    report = _check(path, tmp_path / "out").report
    assert not report.supported
    assert "P&ID" in report.message
    assert report.lines == [] and report.findings == []


def test_part_drawing_without_plant_layers_is_not_called_a_schematic(tmp_path):
    doc = ezdxf.new("R2018")
    doc.modelspace().add_circle((0, 0), 25)
    path = tmp_path / "gear.dxf"
    doc.saveas(path)

    report = _check(path, tmp_path / "out").report
    assert not report.supported
    assert "P&ID" not in report.message
    assert "no layers named like equipment or piping" in report.message


def test_layer_roles_rescue_unconventional_layer_names(plan_layout_dxf, tmp_path):
    doc = ezdxf.readfile(plan_layout_dxf)
    doc.layers.add("M-1")
    doc.layers.add("M-2")
    for entity in doc.modelspace():
        entity.dxf.layer = {"EQUIP-VESSELS": "M-1", "PIPING": "M-2"}.get(
            entity.dxf.layer, entity.dxf.layer
        )
    path = tmp_path / "renamed.dxf"
    doc.saveas(path)

    assert not _infer(path).supported
    roles = AssemblyOptions(layer_roles={"M-1": "equipment", "M-2": "pipe"})
    assert len(_infer(path, roles).obstacles) == 4
