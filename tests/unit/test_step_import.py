from __future__ import annotations

import math

import cadquery as cq
import pytest
from pydantic import ValidationError

from syntera.geometry.step_import import (
    StepImportError,
    import_step_obstacle,
    point_to_imported_distance,
)
from syntera.schemas import DemoConfig, StepObstacle


def step(path, **overrides) -> StepObstacle:
    return StepObstacle.model_validate(
        {"type": "step", "name": "plate", "path": str(path), **overrides}
    )


def test_import_preserves_geometry_and_records_provenance(filleted_plate_step):
    imported = import_step_obstacle(step(filleted_plate_step, translation=[100, 50, 20]))
    summary = imported.summary
    assert summary.solid_count == 1
    assert len(summary.source_sha256) == 64
    assert summary.removed_features == 0
    assert summary.routing_volume_mm3 == pytest.approx(summary.original_volume_mm3)
    box = imported.original.BoundingBox()
    assert (box.xmin, box.ymin, box.zmin) == pytest.approx((40, 10, 0))
    assert (box.xmax, box.ymax, box.zmax) == pytest.approx((160, 90, 40))


def test_defeaturing_only_adds_material(filleted_plate_step):
    imported = import_step_obstacle(
        step(filleted_plate_step, defeaturing={"max_feature_radius": 8})
    )
    summary = imported.summary
    # Four convex rounds and the hole are filled; four concave pocket rounds are kept.
    assert (summary.candidate_features, summary.removed_features) == (9, 5)
    assert summary.rejected_features == 4
    hole = math.pi * 5**2 * 25
    convex_rounds = 4 * (6**2 - math.pi * 6**2 / 4) * 40
    added = summary.routing_volume_mm3 - summary.original_volume_mm3
    assert added == pytest.approx(hole + convex_rounds, rel=1e-6)
    assert imported.original.cut(imported.routing).Volume() == pytest.approx(0, abs=1e-6)


def test_defeaturing_is_deterministic(filleted_plate_step, tmp_path):
    copy = tmp_path / "copy.step"
    copy.write_bytes(filleted_plate_step.read_bytes())
    settings = {"defeaturing": {"max_feature_radius": 8}}
    first = import_step_obstacle(step(filleted_plate_step, **settings)).summary
    second = import_step_obstacle(step(copy, **settings)).summary
    assert first.model_dump(exclude={"source_path"}) == second.model_dump(exclude={"source_path"})


def test_bounding_box_envelope(filleted_plate_step):
    imported = import_step_obstacle(
        step(filleted_plate_step, defeaturing={"envelope": "bounding_box"})
    )
    assert imported.summary.routing_volume_mm3 == pytest.approx(120 * 80 * 40)
    assert point_to_imported_distance((0, 0, 19), imported) == 0.0  # inside the hole
    assert point_to_imported_distance((0, 0, 30), imported) == pytest.approx(10)


def test_point_distance_is_exact(filleted_plate_step):
    imported = import_step_obstacle(step(filleted_plate_step))
    assert point_to_imported_distance((-50, 0, 0), imported) == 0.0
    assert point_to_imported_distance((0, 0, 0), imported) == pytest.approx(5)  # hole axis
    assert point_to_imported_distance((80, 0, 0), imported) == pytest.approx(20)
    corner = point_to_imported_distance((70, 50, 0), imported)
    assert corner == pytest.approx(math.hypot(16, 16) - 6)  # distance to the r6 round


def test_missing_file_fails_closed(tmp_path):
    with pytest.raises(StepImportError, match="does not exist"):
        import_step_obstacle(step(tmp_path / "absent.step"))


def test_unreadable_file_fails_closed(tmp_path):
    path = tmp_path / "broken.step"
    path.write_text("not a STEP file", encoding="utf-8")
    with pytest.raises(StepImportError, match="could not be read"):
        import_step_obstacle(step(path))


def test_surface_only_file_fails_closed(tmp_path):
    path = tmp_path / "face.step"
    cq.exporters.export(cq.Workplane().rect(10, 10).extrude(1).faces(">Z"), str(path))
    with pytest.raises(StepImportError, match="no closed solids"):
        import_step_obstacle(step(path))


def test_non_step_path_is_rejected(base_data, copy_data):
    data = copy_data(base_data)
    data["obstacles"] = [{"type": "step", "name": "pump", "path": "pump.stl"}]
    with pytest.raises(ValidationError, match=r"\.step or \.stp"):
        DemoConfig.model_validate(data)


def test_negative_feature_radius_is_rejected():
    with pytest.raises(ValidationError):
        step("plate.step", defeaturing={"max_feature_radius": -1})


def test_unremovable_blind_bore_is_rejected_and_terminates(vessel_step):
    summary = import_step_obstacle(
        step(vessel_step, defeaturing={"max_feature_radius": 10})
    ).summary
    # Four r8 rounds are filled; the blind bore needs its floor removed too and is kept.
    assert (summary.candidate_features, summary.removed_features) == (5, 4)
    assert summary.rejected_features == 1
