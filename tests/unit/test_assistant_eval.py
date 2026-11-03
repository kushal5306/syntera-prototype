"""The assistant evaluation suite runs in CI with the scripted provider (F5)."""

from __future__ import annotations

import json

import pytest

from syntera.assistant.evaluation import EVAL_FILE, EvalCase, load_cases, run_suite
from syntera.cli import main


def test_every_case_is_answered_with_its_facts_and_citations(tmp_path):
    report = run_suite(tmp_path)
    failures = {c.id: (c.missing_facts, c.missing_citations, c.answer)
                for c in report.cases if not c.passed}  # fmt: skip
    assert failures == {}
    assert report.total == len(load_cases()) >= 8 and report.provider == "scripted"
    calls = {case.id: case.tool_calls for case in report.cases}
    assert calls["od_from_flow"] == ["explain"] and calls["verdict_needs_checks"] == []


def test_a_wrong_expectation_is_reported_per_question(tmp_path):
    case = EvalCase(id="wrong", question="Why 20 mm?", design="examples/demo_skid_sized.yaml",
                    tool="explain", arguments={"path": "tube.outer_diameter"},
                    facts=["25 mm"], citations=["rules.made_up.v1"])  # fmt: skip
    report = run_suite(tmp_path, cases=[case])
    (result,) = report.cases
    assert not result.passed
    assert result.missing_facts == ["25 mm"] and result.missing_citations == ["rules.made_up.v1"]


def test_cli_writes_the_report_and_real_mode_needs_a_key(tmp_path, monkeypatch, capsys):
    assert main(["assistant-eval", "--output", str(tmp_path)]) == 0
    report = json.loads((tmp_path / EVAL_FILE).read_text(encoding="utf-8"))
    assert report["passed"] == report["total"]
    assert "PASS od_from_flow" in capsys.readouterr().out
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        run_suite(tmp_path, "anthropic")


def test_case_ids_are_unique(tmp_path):
    path = tmp_path / "cases.yaml"
    path.write_text("- {id: a, question: q, design: d, facts: [], citations: []}\n" * 2)
    with pytest.raises(ValueError, match="unique"):
        load_cases(path)
