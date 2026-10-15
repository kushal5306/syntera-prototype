from __future__ import annotations

import pytest

from syntera.reporting.compare import compare, fea_validity, piping_cost
from syntera.schemas import (
    AssuranceReport,
    BaselineSkid,
    CostRates,
    FeaAcceptanceReport,
    FeaManifest,
    PackageMetrics,
    PipingMetrics,
)


def metrics(dimensions=(536.0, 274.0, 190.0), length=691.327, bends=4) -> PackageMetrics:
    x, y, z = dimensions
    return PackageMetrics(
        envelope_min_mm=(0, 0, 0),
        envelope_max_mm=dimensions,
        envelope_dimensions_mm=dimensions,
        envelope_volume_m3=x * y * z / 1e9,
        footprint_m2=x * y / 1e6,
        height_mm=z,
        frame_dimensions_mm=(600, 400, 300),
        frame_volume_m3=0.072,
        included_parts=["vessel", "start-port", "end-port", "routed-tube"],
        piping=PipingMetrics(
            main_length_mm=length,
            straight_length_mm=length,
            bend_length_mm=0,
            straights=bends + 1,
            bends=bends,
        ),
    )


def assurance(passed: bool = True) -> AssuranceReport:
    return AssuranceReport(
        route_found=True,
        route_length_mm=691.327,
        number_of_bends=4,
        collision_free=passed,
        minimum_measured_clearance_mm=30,
        required_clearance_mm=20,
        minimum_generated_bend_radius_mm=40,
        required_bend_radius_mm=40,
        start_port_alignment=True,
        end_port_alignment=True,
        execution_time_seconds=1.0,
        overall_pass=passed,
        failure_reasons=[] if passed else ["tube intersects central-process-vessel"],
    )


def baseline(**changes) -> BaselineSkid:
    data = {
        "title": "Reference",
        "synthetic_data": True,
        "source": "test",
        "units": "mm",
        "package_dimensions_mm": [600, 400, 300],
        "main_piping_length_mm": 860,
        "main_piping_bends": 6,
    } | changes
    return BaselineSkid.model_validate(data)


def manifest(generated: bool = True) -> FeaManifest:
    return FeaManifest(
        deck_format_version=1,
        generator="test",
        input_sha256="a" * 64,
        generated=generated,
        diagnostic=None if generated else "route failed assurance",
        allowable_von_mises_mpa=136.7,
        maximum_displacement_mm=1.0,
    )


def acceptance(passed: bool = True, sha: str = "a" * 64) -> FeaAcceptanceReport:
    return FeaAcceptanceReport(
        input_sha256=sha,
        overall_pass=passed,
        load_cases=[],
        failure_reasons=[] if passed else ["thermal_rise: stress exceeds allowable"],
    )


def test_design_identical_to_baseline_gives_ratios_of_exactly_one():
    same = baseline(
        package_dimensions_mm=[536, 274, 190], main_piping_length_mm=691.327, main_piping_bends=4
    )
    report = compare(metrics(), assurance(), same, "0" * 64, [])
    assert report.package_volume_ratio == 1.0
    assert report.footprint_ratio == 1.0
    assert report.height_ratio == 1.0
    assert report.piping_length_ratio == 1.0
    assert report.piping_length_saving_mm == 0.0
    assert report.piping_cost_saving == 0.0
    assert report.design_valid is True
    assert report.kpi2_met is False  # equal is not smaller


def test_hand_calculated_ratios_and_cost_saving():
    report = compare(metrics(), assurance(), baseline(), "0" * 64, [])
    assert report.package_volume_ratio == pytest.approx(536 * 274 * 190 / (600 * 400 * 300))
    assert report.footprint_ratio == pytest.approx(536 * 274 / (600 * 400))
    assert report.height_ratio == pytest.approx(190 / 300)
    assert report.piping_length_saving_mm == pytest.approx(168.673)
    # baseline 0.86 m x 120 + 6 x 45 = 373.20; design 0.691327 m x 120 + 4 x 45 = 262.96
    assert report.baseline.estimated_piping_cost == 373.20
    assert report.design.estimated_piping_cost == 262.96
    assert report.piping_cost_saving == 110.24
    assert report.kpi2_met is True


def test_failed_assurance_never_counts_even_when_smaller():
    report = compare(metrics(), assurance(passed=False), baseline(), "0" * 64, [])
    assert report.package_volume_ratio < 1
    assert report.design_valid is False
    assert report.kpi2_met is False
    assert report.validity_reasons == ["assurance failed: tube intersects central-process-vessel"]


def test_fea_evidence_fails_closed():
    assert fea_validity(None, None) == []
    assert fea_validity(manifest(), acceptance()) == []
    assert "not been evaluated" in fea_validity(manifest(), None)[0]
    assert "does not match" in fea_validity(manifest(), acceptance(sha="b" * 64))[0]
    assert "acceptance failed" in fea_validity(manifest(), acceptance(passed=False))[0]
    assert "not generated" in fea_validity(manifest(generated=False), None)[0]
    report = compare(metrics(), assurance(), baseline(), "0" * 64, fea_validity(manifest(), None))
    assert report.kpi2_met is False


def test_piping_cost_uses_shared_rates():
    rates = CostRates(cost_per_metre=200, cost_per_bend=10, currency="EUR")
    assert piping_cost(1500, 3, rates) == 330.0
    assert piping_cost(None, 3, rates) is None


def test_baseline_rejects_non_millimetre_units():
    with pytest.raises(ValueError):
        baseline(units="in")
