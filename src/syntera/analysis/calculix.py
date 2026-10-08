"""Versioned, byte-for-byte deterministic CalculiX input-deck generation."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable
from pathlib import Path

from syntera import __version__
from syntera.analysis.mesh import ShellMesh, mesh_tube
from syntera.schemas import (
    AnalysisSpecification,
    DemoConfig,
    FeaLoadCaseDeck,
    FeaManifest,
    FeaMeshSummary,
    LoadCase,
    Point3,
)

DECK_FORMAT_VERSION = 1
GENERATOR = f"syntera-ccx-deck/{DECK_FORMAT_VERSION} (syntera {__version__})"
MESH_FILE = "mesh.inp"
MANIFEST_FILE = "fea_manifest.json"
REFERENCE_TEMPERATURE_K = 293.15
_IDS_PER_LINE = 16


def input_sha256(config: DemoConfig, points: list[Point3]) -> str:
    """Hash the validated configuration and routed centreline for provenance."""
    payload = {"config": config.model_dump(mode="json"), "route": [list(p) for p in points]}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _number(value: float) -> str:
    """Format a float deterministically and without a negative zero."""
    text = f"{value:.9g}"
    return "0" if text in {"0", "-0"} else text


def _coordinate(value: float) -> str:
    rounded = round(value, 6) + 0.0
    return f"{rounded:.6f}"


def _id_lines(ids: Iterable[int]) -> list[str]:
    values = list(ids)
    return [
        ", ".join(str(item) for item in values[start : start + _IDS_PER_LINE])
        for start in range(0, len(values), _IDS_PER_LINE)
    ]


def _header(title: str, digest: str) -> list[str]:
    return [
        f"** {title}",
        f"** generator: {GENERATOR}",
        f"** input sha256: {digest}",
        "** units: mm, N, s, tonne, MPa, K",
        "** synthetic demonstration data; not certified for production or safety use",
    ]


def mesh_deck(mesh: ShellMesh, digest: str) -> str:
    lines = _header("Syntera tube mid-surface mesh", digest)
    lines.append("*NODE, NSET=NALL")
    lines.extend(
        f"{index}, " + ", ".join(_coordinate(value) for value in point)
        for index, point in enumerate(mesh.nodes, start=1)
    )
    lines.append("*ELEMENT, TYPE=S8R, ELSET=ETUBE")
    lines.extend(
        f"{index}, " + ", ".join(str(node) for node in element)
        for index, element in enumerate(mesh.elements, start=1)
    )
    lines.append("*NSET, NSET=NSTART")
    lines.extend(_id_lines(mesh.start_ring))
    lines.append("*NSET, NSET=NEND")
    lines.extend(_id_lines(mesh.end_ring))
    return "\n".join(lines) + "\n"


def load_case_deck(analysis: AnalysisSpecification, case: LoadCase, digest: str) -> str:
    """Build one static step with both tube ends clamped at their ports."""
    material = analysis.material
    lines = _header(f"Syntera load case {case.name}", digest)
    lines += ["*HEADING", f"Syntera tube load case {case.name}", f"*INCLUDE, INPUT={MESH_FILE}"]
    lines += [
        f"*MATERIAL, NAME={material.name.upper()}",
        "*ELASTIC",
        f"{_number(material.elastic_modulus_mpa)}, {_number(material.poisson_ratio)}",
    ]
    if material.density_tonne_per_mm3 is not None:
        lines += ["*DENSITY", _number(material.density_tonne_per_mm3)]
    if material.thermal_expansion_per_k is not None:
        lines += [
            f"*EXPANSION, ZERO={_number(REFERENCE_TEMPERATURE_K)}",
            _number(material.thermal_expansion_per_k),
        ]
    lines += [
        f"*SHELL SECTION, ELSET=ETUBE, MATERIAL={material.name.upper()}",
        _number(analysis.wall_thickness),
        "*INITIAL CONDITIONS, TYPE=TEMPERATURE",
        f"NALL, {_number(REFERENCE_TEMPERATURE_K)}",
        "*STEP",
        "*STATIC",
        "*BOUNDARY",
        "NSTART, 1, 6",
        "NEND, 1, 6",
    ]
    loads: list[str] = []
    if case.internal_pressure_mpa > 0:
        loads.append(f"ETUBE, P, {_number(case.internal_pressure_mpa)}")
    if case.gravity_mm_per_s2 is not None and any(case.gravity_mm_per_s2):
        magnitude = math.sqrt(sum(value * value for value in case.gravity_mm_per_s2))
        direction = ", ".join(_number(value / magnitude) for value in case.gravity_mm_per_s2)
        loads.append(f"ETUBE, GRAV, {_number(magnitude)}, {direction}")
    if loads:
        lines += ["*DLOAD", *loads]
    lines += [
        "*TEMPERATURE",
        f"NALL, {_number(REFERENCE_TEMPERATURE_K + case.temperature_change_k)}",
        "*NODE PRINT, NSET=NALL",
        "U",
        "*EL PRINT, ELSET=ETUBE",
        "S",
        "*NODE FILE",
        "U",
        "*EL FILE",
        "S",
        "*END STEP",
    ]
    return "\n".join(lines) + "\n"


def write_fea_inputs(config: DemoConfig, points: list[Point3], output: Path) -> FeaManifest:
    """Mesh the routed tube and write one CalculiX deck per load case plus a manifest.

    Meshing failures are reported in the manifest and no decks are written (fail closed).
    """
    analysis = config.analysis
    if analysis is None:
        raise ValueError("configuration has no analysis section")
    output.mkdir(parents=True, exist_ok=True)
    digest = input_sha256(config, points)
    manifest = FeaManifest(
        deck_format_version=DECK_FORMAT_VERSION,
        generator=GENERATOR,
        input_sha256=digest,
        generated=False,
        allowable_von_mises_mpa=(
            analysis.material.yield_strength_mpa / analysis.acceptance.stress_safety_factor
        ),
        maximum_displacement_mm=analysis.acceptance.maximum_displacement_mm,
    )
    try:
        mesh = mesh_tube(
            points,
            config.tube.minimum_bend_radius,
            config.tube.outer_diameter,
            analysis.wall_thickness,
            analysis.mesh,
        )
    except ValueError as error:
        manifest = manifest.model_copy(update={"diagnostic": f"meshing failed safely: {error}"})
    else:
        (output / MESH_FILE).write_text(mesh_deck(mesh, digest), encoding="utf-8")
        decks: list[FeaLoadCaseDeck] = []
        for case in analysis.load_cases:
            deck_file = f"{case.name}.inp"
            (output / deck_file).write_text(
                load_case_deck(analysis, case, digest), encoding="utf-8"
            )
            decks.append(
                FeaLoadCaseDeck(name=case.name, deck_file=deck_file, result_file=f"{case.name}.dat")
            )
        manifest = manifest.model_copy(
            update={
                "generated": True,
                "mesh_file": MESH_FILE,
                "mesh": FeaMeshSummary(
                    element_type="S8R",
                    nodes=len(mesh.nodes),
                    elements=len(mesh.elements),
                    axial_elements=mesh.axial_elements,
                    circumferential_elements=mesh.circumferential_elements,
                    mid_surface_radius_mm=mesh.mid_surface_radius,
                    maximum_aspect_ratio=mesh.maximum_aspect_ratio,
                ),
                "load_cases": decks,
            }
        )
    (output / MANIFEST_FILE).write_text(
        json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest
