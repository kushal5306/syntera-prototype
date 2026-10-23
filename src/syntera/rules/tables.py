"""Synthetic engineering tables used by the rule library.

Every value here is invented demonstration data chosen to be plausible for small-bore
skid tubing. None of it is taken from, or certified against, a design code or a supplier
catalogue, and it must be replaced with licensed data before any real use.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from syntera.schemas import Material

TABLES_VERSION = 1
SYNTHETIC_TABLE_NOTICE = (
    "Synthetic Syntera demonstration table v1: not from, or certified against, any design code"
)

Medium = Literal["water", "oil", "air", "steam"]


class TubeSize(BaseModel):
    """One standard tube outside diameter and the wall thicknesses stocked for it (mm)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outer_diameter: float = Field(gt=0)
    walls: tuple[float, ...] = Field(min_length=1)


class MediumLimits(BaseModel):
    """Velocity limit for one medium."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    medium: Medium
    maximum_velocity_m_per_s: float = Field(gt=0)


class MaterialBand(BaseModel):
    """Material recommended for a medium up to a temperature, with its allowable stress."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    medium: Medium
    maximum_temperature_c: float
    material: Material
    allowable_stress_mpa: float = Field(gt=0)


# Ascending outside diameter; walls ascending for each size.
TUBE_SIZES: tuple[TubeSize, ...] = (
    TubeSize(outer_diameter=6, walls=(1.0,)),
    TubeSize(outer_diameter=8, walls=(1.0, 1.5)),
    TubeSize(outer_diameter=10, walls=(1.0, 1.5, 2.0)),
    TubeSize(outer_diameter=12, walls=(1.0, 1.5, 2.0)),
    TubeSize(outer_diameter=16, walls=(1.0, 1.5, 2.0)),
    TubeSize(outer_diameter=20, walls=(1.0, 1.5, 2.0, 2.5)),
    TubeSize(outer_diameter=25, walls=(1.5, 2.0, 2.5, 3.0)),
    TubeSize(outer_diameter=30, walls=(1.5, 2.0, 3.0)),
    TubeSize(outer_diameter=38, walls=(1.5, 2.0, 3.0, 4.0)),
    TubeSize(outer_diameter=50, walls=(2.0, 3.0, 4.0, 5.0)),
)

MEDIUM_LIMITS: tuple[MediumLimits, ...] = (
    MediumLimits(medium="water", maximum_velocity_m_per_s=2.0),
    MediumLimits(medium="oil", maximum_velocity_m_per_s=1.5),
    MediumLimits(medium="air", maximum_velocity_m_per_s=15.0),
    MediumLimits(medium="steam", maximum_velocity_m_per_s=25.0),
)

_STAINLESS_316 = Material(
    name="stainless_316",
    elastic_modulus_mpa=193000,
    poisson_ratio=0.3,
    yield_strength_mpa=205,
    density_tonne_per_mm3=8.0e-9,
    thermal_expansion_per_k=1.6e-5,
)
_CARBON_STEEL = Material(
    name="carbon_steel",
    elastic_modulus_mpa=200000,
    poisson_ratio=0.3,
    yield_strength_mpa=240,
    density_tonne_per_mm3=7.85e-9,
    thermal_expansion_per_k=1.2e-5,
)

# For each medium, bands in ascending temperature; the first band that covers the design
# temperature is chosen. Allowable stresses fall with temperature.
MATERIAL_BANDS: tuple[MaterialBand, ...] = (
    MaterialBand(
        medium="water", maximum_temperature_c=100, material=_STAINLESS_316, allowable_stress_mpa=138
    ),
    MaterialBand(
        medium="water", maximum_temperature_c=200, material=_STAINLESS_316, allowable_stress_mpa=120
    ),
    MaterialBand(
        medium="oil", maximum_temperature_c=150, material=_CARBON_STEEL, allowable_stress_mpa=138
    ),
    MaterialBand(
        medium="oil", maximum_temperature_c=300, material=_CARBON_STEEL, allowable_stress_mpa=118
    ),
    MaterialBand(
        medium="air", maximum_temperature_c=200, material=_CARBON_STEEL, allowable_stress_mpa=138
    ),
    MaterialBand(
        medium="steam", maximum_temperature_c=250, material=_STAINLESS_316, allowable_stress_mpa=115
    ),
    MaterialBand(
        medium="steam", maximum_temperature_c=400, material=_STAINLESS_316, allowable_stress_mpa=100
    ),
)
