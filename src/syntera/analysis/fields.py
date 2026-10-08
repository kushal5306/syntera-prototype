"""Solve generated CalculiX decks and read per-node and per-element result fields.

The acceptance verdict always comes from :func:`syntera.analysis.results.evaluate_acceptance`;
these fields only feed the stress and displacement views in the browser.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from syntera.analysis.results import _leading_numbers, von_mises
from syntera.schemas import FeaManifest


@dataclass(frozen=True)
class ShellModel:
    nodes: list[list[float]]
    elements: list[list[int]]


@dataclass(frozen=True)
class CaseFields:
    name: str
    displacement: list[list[float]]
    element_von_mises: list[float]


@dataclass(frozen=True)
class SolveOutcome:
    solved: bool
    diagnostic: str | None = None


def parse_mesh_inp(text: str) -> ShellModel:
    """Read ``*NODE`` coordinates and ``*ELEMENT`` connectivity (zero-based node indices)."""
    nodes: list[list[float]] = []
    elements: list[list[int]] = []
    block: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("**"):
            continue
        if line.startswith("*"):
            keyword = line.split(",")[0].upper()
            block = {"*NODE": "node", "*ELEMENT": "element"}.get(keyword)
            continue
        values = [item.strip() for item in line.split(",") if item.strip()]
        if block == "node":
            nodes.append([float(value) for value in values[1:4]])
        elif block == "element":
            elements.append([int(value) - 1 for value in values[1:]])
    return ShellModel(nodes=nodes, elements=elements)


def parse_case_fields(name: str, text: str, model: ShellModel) -> CaseFields:
    """Nodal displacement vectors and peak integration-point von Mises stress per element."""
    displacement = [[0.0, 0.0, 0.0] for _ in model.nodes]
    stress = [0.0 for _ in model.elements]
    block: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        lowered = line.lower()
        if lowered.startswith("displacements"):
            block = "u"
            continue
        if lowered.startswith("stresses"):
            block = "s"
            continue
        values = _leading_numbers(line)
        if not values:
            block = None
            continue
        if block == "u" and len(values) == 4:
            displacement[int(values[0]) - 1] = [round(value, 6) for value in values[1:]]
        elif block == "s" and len(values) == 8:
            element = int(values[0]) - 1
            stress[element] = max(stress[element], round(von_mises(*values[2:]), 3))
    return CaseFields(name=name, displacement=displacement, element_von_mises=stress)


def solve_decks(fea_dir: Path, manifest: FeaManifest, timeout_s: float = 120.0) -> SolveOutcome:
    """Run ``ccx`` on every deck in the manifest; any failure is reported, never hidden."""
    executable = shutil.which("ccx")
    if executable is None:
        return SolveOutcome(False, "CalculiX (ccx) is not installed on the server")
    if not manifest.generated:
        return SolveOutcome(False, manifest.diagnostic or "CalculiX decks were not generated")
    for deck in manifest.load_cases:
        try:
            completed = subprocess.run(
                [executable, "-i", Path(deck.deck_file).stem],
                cwd=fea_dir,
                capture_output=True,
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return SolveOutcome(False, f"{deck.name}: CalculiX exceeded {timeout_s:.0f} s")
        if completed.returncode != 0 or not (fea_dir / deck.result_file).is_file():
            return SolveOutcome(False, f"{deck.name}: CalculiX exited with {completed.returncode}")
    return SolveOutcome(True)
