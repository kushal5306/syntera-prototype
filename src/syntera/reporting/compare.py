"""Compare a routed Syntera design with a baseline skid for KPI 2 (package size and piping).

The ratios are plain arithmetic on ``metrics.json``. Whether they count is decided by the
deterministic checks already on disk: the assurance report must pass, and when FEA decks
were generated their acceptance report must exist, pass, and match the decks' input hash.
Anything missing fails closed, so a smaller design that was never fully checked is never
reported as meeting KPI 2.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

from syntera.analysis.calculix import MANIFEST_FILE
from syntera.schemas import (
    AssuranceReport,
    BaselineSkid,
    ComparisonReport,
    CostRates,
    FeaAcceptanceReport,
    FeaManifest,
    PackageFigures,
    PackageMetrics,
)

COMPARISON_FILE = "comparison.json"
FEA_ACCEPTANCE_FILE = "fea_acceptance_report.json"


def load_baseline(path: Path) -> tuple[BaselineSkid, str]:
    """Validated baseline plus the SHA-256 of the exact file it came from."""
    data = path.read_bytes()
    baseline = BaselineSkid.model_validate(yaml.safe_load(data.decode("utf-8")))
    return baseline, hashlib.sha256(data).hexdigest()


def piping_cost(length_mm: float | None, bends: int | None, rates: CostRates) -> float | None:
    if length_mm is None or bends is None:
        return None
    return round(length_mm / 1000 * rates.cost_per_metre + bends * rates.cost_per_bend, 2)


def baseline_figures(baseline: BaselineSkid) -> PackageFigures:
    x, y, z = baseline.package_dimensions_mm
    return PackageFigures(
        package_dimensions_mm=(x, y, z),
        package_volume_m3=x * y * z / 1e9,
        footprint_m2=x * y / 1e6,
        height_mm=z,
        main_piping_length_mm=baseline.main_piping_length_mm,
        main_piping_bends=baseline.main_piping_bends,
        estimated_piping_cost=piping_cost(
            baseline.main_piping_length_mm, baseline.main_piping_bends, baseline.rates
        ),
    )


def design_figures(metrics: PackageMetrics, rates: CostRates) -> PackageFigures:
    piping = metrics.piping
    length = piping.main_length_mm if piping else None
    bends = piping.bends if piping else None
    return PackageFigures(
        package_dimensions_mm=metrics.envelope_dimensions_mm,
        package_volume_m3=metrics.envelope_volume_m3,
        footprint_m2=metrics.footprint_m2,
        height_mm=metrics.height_mm,
        main_piping_length_mm=length,
        main_piping_bends=bends,
        estimated_piping_cost=piping_cost(length, bends, rates),
    )


def fea_validity(manifest: FeaManifest | None, acceptance: FeaAcceptanceReport | None) -> list[str]:
    """Reasons the FEA evidence is missing or failing; empty when it passes or was not set up."""
    if manifest is None:
        return []
    if not manifest.generated:
        return [f"FEA decks were not generated: {manifest.diagnostic or 'no diagnostic'}"]
    if acceptance is None:
        return ["FEA acceptance has not been evaluated; solve the decks and run fea-evaluate"]
    if acceptance.input_sha256 != manifest.input_sha256:
        return ["FEA acceptance report does not match the current decks"]
    if not acceptance.overall_pass:
        return ["FEA acceptance failed: " + "; ".join(acceptance.failure_reasons)]
    return []


def _ratio(design: float, baseline: float) -> float:
    return round(design / baseline, 6)


def compare(
    metrics: PackageMetrics,
    assurance: AssuranceReport,
    baseline: BaselineSkid,
    baseline_sha256: str,
    fea_reasons: list[str],
) -> ComparisonReport:
    base = baseline_figures(baseline)
    design = design_figures(metrics, baseline.rates)
    reasons = [f"assurance failed: {reason}" for reason in assurance.failure_reasons]
    if not assurance.overall_pass and not reasons:
        reasons.append("assurance failed")
    if design.main_piping_length_mm is None:
        reasons.append("no routed main line to compare")
    reasons.extend(fea_reasons)
    valid = not reasons
    volume_ratio = _ratio(design.package_volume_m3, base.package_volume_m3)
    length = design.main_piping_length_mm
    base_length = baseline.main_piping_length_mm
    cost_saving = (
        None
        if design.estimated_piping_cost is None or base.estimated_piping_cost is None
        else round(base.estimated_piping_cost - design.estimated_piping_cost, 2)
    )
    return ComparisonReport(
        baseline_title=baseline.title,
        baseline_synthetic=baseline.synthetic_data,
        baseline_source=baseline.source,
        baseline_sha256=baseline_sha256,
        currency=baseline.rates.currency,
        baseline=base,
        design=design,
        package_volume_ratio=volume_ratio,
        footprint_ratio=_ratio(design.footprint_m2, base.footprint_m2),
        height_ratio=_ratio(design.height_mm, base.height_mm),
        piping_length_ratio=None if length is None else _ratio(length, base_length),
        piping_length_saving_mm=None if length is None else round(base_length - length, 3),
        piping_cost_saving=cost_saving,
        design_valid=valid,
        validity_reasons=reasons,
        kpi2_met=valid and volume_ratio < 1,
    )


def compare_run(output: Path, baseline_path: Path) -> ComparisonReport:
    """Compare the run stored in ``output`` (from ``syntera demo``) with a baseline file."""
    metrics = PackageMetrics.model_validate_json((output / "metrics.json").read_text("utf-8"))
    assurance = AssuranceReport.model_validate_json(
        (output / "assurance_report.json").read_text("utf-8")
    )
    manifest_path = output / "fea" / MANIFEST_FILE
    acceptance_path = output / "fea" / FEA_ACCEPTANCE_FILE
    manifest = (
        FeaManifest.model_validate_json(manifest_path.read_text("utf-8"))
        if manifest_path.is_file()
        else None
    )
    acceptance = (
        FeaAcceptanceReport.model_validate_json(acceptance_path.read_text("utf-8"))
        if acceptance_path.is_file()
        else None
    )
    baseline, digest = load_baseline(baseline_path)
    return compare(metrics, assurance, baseline, digest, fea_validity(manifest, acceptance))
