"""Standard component library: tables, solids, placement and exact checks (B2)."""

from __future__ import annotations

import math
from pathlib import Path

import cadquery as cq
import pytest

from syntera.components.fittings import (
    make_elbow,
    make_flange,
    make_reducer,
    make_tee,
    place_fittings,
    verify_fittings,
)
from syntera.components.tables import (
    FLANGES,
    TABLE_ODS,
    TableError,
    flange_for,
    reducer_for,
    tee_for,
)
from syntera.config import load_config
from syntera.geometry.centreline import centreline_segments
from syntera.schemas import FittingSpec

PROJECT = Path(__file__).parents[2]
CONFIG = load_config(PROJECT / "examples" / "demo_skid_fittings.yaml")
ROUTE = [(40.0, 200.0, 100.0), (200.0, 200.0, 100.0), (200.0, 320.0, 100.0),
         (560.0, 320.0, 100.0), (560.0, 200.0, 100.0)]  # fmt: skip


def test_tables_cover_every_size_and_rating_with_sane_proportions():
    for od in TABLE_ODS:
        for rating in ("PN16", "PN40"):
            flange = flange_for(od, rating)
            assert flange.outer_diameter > od + 2 * flange.bolt_hole
            assert od / 2 < flange.bolt_circle / 2 - flange.bolt_hole / 2
            assert flange.bolt_circle / 2 + flange.bolt_hole / 2 < flange.outer_diameter / 2
        assert flange_for(od, "PN40").thickness > flange_for(od, "PN16").thickness
    assert len(FLANGES) == 2 * len(TABLE_ODS)
    with pytest.raises(TableError, match="no PN16 flange for a 21 mm tube"):
        flange_for(21, "PN16")
    with pytest.raises(TableError, match="branch cannot be larger"):
        tee_for(20, 25)
    with pytest.raises(TableError, match="small end must be smaller"):
        reducer_for(20, 20)


def test_solids_have_the_tabled_size():
    size = flange_for(20, "PN16")
    flange = make_flange(size, (0, 0, 0), (1, 0, 0))
    box = flange.BoundingBox()
    assert (box.xlen, box.ylen, box.zlen) == pytest.approx(
        (size.thickness, size.outer_diameter, size.outer_diameter), abs=1e-3
    )
    ring = math.pi / 4 * (size.outer_diameter**2 - size.tube_od**2) * size.thickness
    holes = size.bolts * math.pi / 4 * size.bolt_hole**2 * size.thickness
    assert flange.Volume() == pytest.approx(ring - holes, rel=1e-6)
    tee = make_tee(20, 12, (0, 0, 0), (1, 0, 0), (0, 0, 1))
    assert tee.BoundingBox().zmax == pytest.approx(tee_for(20, 12).branch_centre_to_end)
    reducer = make_reducer(20, 12, (0, 0, 0), (0, 0, 1))
    assert reducer.BoundingBox().zlen == pytest.approx(reducer_for(20, 12).length)
    arc = next(s for s in centreline_segments(ROUTE, 40) if hasattr(s, "radius"))
    elbow = make_elbow(arc, 20)
    assert elbow.Volume() == pytest.approx(math.pi * 10**2 * arc.length, rel=1e-3)


def test_placement_puts_elbows_at_bends_and_flanges_at_ends():
    placed = place_fittings(ROUTE, 20, 40, FittingSpec(), start_direction=(1, 0, 0),
                            end_direction=(0, 1, 0))  # fmt: skip
    kinds = [(f.id, f.kind) for f in placed.report.fittings]
    assert kinds == [
        ("main-E1", "pipe_bend"),
        ("main-E2", "pipe_bend"),
        ("main-E3", "pipe_bend"),
        ("main-F1", "flange"),
        ("main-F2", "flange"),
    ]
    standard = place_fittings(ROUTE, 20, 30, FittingSpec(end_connection="welded"))
    assert {f.kind for f in standard.report.fittings} == {"elbow_lr"}
    # straight cut lengths plus elbow arcs equal the centreline less the two flanges
    centreline = sum(s.length for s in centreline_segments(ROUTE, 40))
    straight = sum(p.length_mm for p in placed.report.straight_pieces)
    arcs = sum(f.length_mm for f in placed.report.fittings if f.kind != "flange")
    thickness = flange_for(20, "PN16").thickness
    assert straight + arcs == pytest.approx(centreline - 2 * thickness, abs=1e-5)
    f2 = next(f for f in placed.report.fittings if f.id == "main-F2")
    assert f2.direction == (0, 1, 0) and f2.position == ROUTE[-1]


def test_fitting_checks_pass_in_clear_space_and_fail_on_a_clash():
    placed = place_fittings([(40.0, 200.0, 100.0), (560.0, 200.0, 100.0)], 20, 40,
                            FittingSpec(), start_direction=(1, 0, 0),
                            end_direction=(-1, 0, 0))  # fmt: skip
    report = verify_fittings(CONFIG, placed)
    assert report.overall_pass, report.failure_reasons
    assert all(check.minimum_clearance_mm >= 20 for check in report.checks)

    data = CONFIG.model_dump(mode="json")
    data["obstacles"].append({"type": "box", "name": "bracket", "center": [48, 245, 100],
                              "size": [10, 10, 20]})  # fmt: skip
    clashing = type(CONFIG).model_validate(data)
    failed = verify_fittings(clashing, placed)
    assert not failed.overall_pass
    assert failed.failure_reasons == ["fitting main-F1 (flange) intersects an obstacle"]


def test_compound_of_fittings_is_a_valid_shape():
    placed = place_fittings(ROUTE, 20, 30, FittingSpec())
    compound = cq.Compound.makeCompound(list(placed.shapes.values()))
    assert compound.isValid()
