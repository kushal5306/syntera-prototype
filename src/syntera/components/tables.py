"""Synthetic fitting dimension tables keyed by tube OD (mm) and rating.

Every value is invented demonstration data loosely proportioned like real fittings. None
of it comes from, or is certified against, a fitting standard or supplier catalogue.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

COMPONENT_TABLES_VERSION = 1
SYNTHETIC_COMPONENT_NOTICE = (
    "Synthetic Syntera fitting tables v1: not from, or certified against, any fitting standard"
)
LONG_RADIUS_FACTOR = 1.5

Rating = Literal["PN16", "PN40"]


class FlangeSize(BaseModel):
    """A flat-faced slip-on flange for one tube size and rating (mm)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tube_od: float = Field(gt=0)
    rating: Rating
    outer_diameter: float = Field(gt=0)
    thickness: float = Field(gt=0)
    bolt_circle: float = Field(gt=0)
    bolts: int = Field(ge=4)
    bolt_hole: float = Field(gt=0)


def _flange(tube_od: float, rating: Rating) -> FlangeSize:
    heavy = rating == "PN40"
    outer = round(tube_od * 2.2 + 50 + (5 if heavy else 0))
    hole = 11.0 if tube_od < 25 else 14.0
    return FlangeSize(
        tube_od=tube_od,
        rating=rating,
        outer_diameter=outer,
        thickness=round(10 + tube_od * 0.15 + (4 if heavy else 0), 1),
        bolt_circle=round(outer - 2 * hole - 6),
        bolts=4 if tube_od < 38 else 8,
        bolt_hole=hole,
    )


TABLE_ODS: tuple[float, ...] = (6, 8, 10, 12, 16, 20, 25, 30, 38, 50)
FLANGES: dict[tuple[Rating, float], FlangeSize] = {
    (rating, od): _flange(od, rating) for rating in ("PN16", "PN40") for od in TABLE_ODS
}


class TeeSize(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_od: float = Field(gt=0)
    branch_od: float = Field(gt=0)
    run_centre_to_end: float = Field(gt=0)
    branch_centre_to_end: float = Field(gt=0)


class ReducerSize(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    large_od: float = Field(gt=0)
    small_od: float = Field(gt=0)
    length: float = Field(gt=0)


class TableError(ValueError):
    """No table row for the requested size."""


def flange_for(tube_od: float, rating: Rating) -> FlangeSize:
    row = FLANGES.get((rating, float(tube_od)))
    if row is None:
        raise TableError(f"no {rating} flange for a {tube_od:g} mm tube in the synthetic table")
    return row


def tee_for(run_od: float, branch_od: float) -> TeeSize:
    if float(run_od) not in TABLE_ODS or float(branch_od) not in TABLE_ODS:
        raise TableError(f"no tee for {run_od:g} x {branch_od:g} mm in the synthetic table")
    if branch_od > run_od:
        raise TableError("a tee branch cannot be larger than its run")
    return TeeSize(
        run_od=run_od,
        branch_od=branch_od,
        run_centre_to_end=round(1.1 * run_od + 10, 1),
        branch_centre_to_end=round(1.1 * run_od + 10, 1),
    )


def reducer_for(large_od: float, small_od: float) -> ReducerSize:
    if float(large_od) not in TABLE_ODS or float(small_od) not in TABLE_ODS:
        raise TableError(f"no reducer for {large_od:g} x {small_od:g} mm in the synthetic table")
    if small_od >= large_od:
        raise TableError("a reducer's small end must be smaller than its large end")
    return ReducerSize(
        large_od=large_od, small_od=small_od, length=round(2 * (large_od - small_od) + 20, 1)
    )
