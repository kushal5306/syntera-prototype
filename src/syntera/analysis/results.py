"""Deterministic parsing of CalculiX ``.dat`` results and acceptance evaluation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from syntera.schemas import FeaAcceptanceReport, FeaCaseAcceptance, FeaManifest


@dataclass(frozen=True)
class CaseResults:
    maximum_von_mises_mpa: float | None
    maximum_displacement_mm: float | None


def von_mises(sxx: float, syy: float, szz: float, sxy: float, sxz: float, syz: float) -> float:
    return math.sqrt(
        0.5 * ((sxx - syy) ** 2 + (syy - szz) ** 2 + (szz - sxx) ** 2)
        + 3.0 * (sxy**2 + sxz**2 + syz**2)
    )


def _leading_numbers(line: str) -> list[float]:
    """Numbers before any trailing label (CalculiX 2.21 appends a part name to stresses)."""
    values: list[float] = []
    for item in line.split():
        try:
            values.append(float(item))
        except ValueError:
            break
    return values


def parse_dat(text: str) -> CaseResults:
    """Extract peak displacement magnitude and peak von Mises stress from ``.dat`` text."""
    block: str | None = None
    stress: float | None = None
    displacement: float | None = None
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
            magnitude = math.sqrt(sum(value * value for value in values[1:]))
            displacement = magnitude if displacement is None else max(displacement, magnitude)
        elif block == "s" and len(values) == 8:
            equivalent = von_mises(*values[2:])
            stress = equivalent if stress is None else max(stress, equivalent)
    return CaseResults(maximum_von_mises_mpa=stress, maximum_displacement_mm=displacement)


def _check(case: str, results: CaseResults | None, manifest: FeaManifest) -> FeaCaseAcceptance:
    reasons: list[str] = []
    stress = results.maximum_von_mises_mpa if results else None
    displacement = results.maximum_displacement_mm if results else None
    if results is None:
        reasons.append(f"{case}: CalculiX result file is missing")
    else:
        if stress is None or not math.isfinite(stress):
            reasons.append(f"{case}: no finite stress results were found")
        elif stress > manifest.allowable_von_mises_mpa:
            reasons.append(
                f"{case}: von Mises stress {stress:.3f} MPa exceeds allowable "
                f"{manifest.allowable_von_mises_mpa:.3f} MPa"
            )
        if displacement is None or not math.isfinite(displacement):
            reasons.append(f"{case}: no finite displacement results were found")
        elif displacement > manifest.maximum_displacement_mm:
            reasons.append(
                f"{case}: displacement {displacement:.4f} mm exceeds limit "
                f"{manifest.maximum_displacement_mm:.4f} mm"
            )
    return FeaCaseAcceptance(
        name=case,
        maximum_von_mises_mpa=stress,
        allowable_von_mises_mpa=manifest.allowable_von_mises_mpa,
        maximum_displacement_mm=displacement,
        allowed_displacement_mm=manifest.maximum_displacement_mm,
        passed=not reasons,
        failure_reasons=reasons,
    )


def evaluate_acceptance(
    manifest: FeaManifest, fea_dir: Path, expected_sha256: str
) -> FeaAcceptanceReport:
    """Apply the manifest thresholds to every load case; anything missing fails closed."""
    reasons: list[str] = []
    if manifest.input_sha256 != expected_sha256:
        reasons.append("FEA manifest does not match the current configuration and route")
    if not manifest.generated:
        reasons.append(manifest.diagnostic or "CalculiX decks were not generated")
    elif not manifest.load_cases:
        reasons.append("FEA manifest lists no load cases")
    cases: list[FeaCaseAcceptance] = []
    for deck in manifest.load_cases:
        result_path = fea_dir / deck.result_file
        results = None
        if result_path.is_file():
            results = parse_dat(result_path.read_text(encoding="utf-8"))
        case = _check(deck.name, results, manifest)
        cases.append(case)
        reasons.extend(case.failure_reasons)
    return FeaAcceptanceReport(
        input_sha256=manifest.input_sha256,
        overall_pass=not reasons,
        load_cases=cases,
        failure_reasons=reasons,
    )
