from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from syntera.reporting.timelog import (
    ParsedLog,
    append_event,
    kpi1_report,
    load_manual_baseline,
    make_event,
    pair_intervals,
    read_log,
    union_hours,
)
from syntera.schemas import ManualBaseline, TimeEvent

PROJECT_ROOT = Path(__file__).parents[2]
T0 = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def span(task, phase, start, stop, actor="engineer"):
    return [
        make_event(task, phase, "start", actor=actor, at=at(start)),
        make_event(task, phase, "stop", actor=actor, at=at(stop)),
    ]


def baseline(*tasks: tuple[str, float]) -> ManualBaseline:
    return ManualBaseline(
        title="test",
        synthetic_data=True,
        source="hand-made",
        tasks=[
            {
                "task": name,
                "description": name,
                "phases": [{"phase": "routing", "hours": hours, "estimate": False}],
            }
            for name, hours in tasks
        ],
    )


def report(events, manual):
    return kpi1_report(ParsedLog(events=events, sha256="0" * 64, errors=[]), manual, "1" * 64)


def test_synthetic_log_gives_the_hand_calculated_ratio():
    # Review 0-30 min with a 6 min regeneration inside it, then 15 min of edits:
    # 45 min of Syntera workflow time against 8 manual hours.
    events = (
        span("one-line", "review", 0, 30)
        + span("one-line", "generate", 10, 16, actor="syntera")
        + span("one-line", "edit", 40, 55)
    )
    result = report(events, baseline(("one-line", 8.0)))
    task = result.tasks[0]
    assert task.measured is True
    assert task.ai_hours == pytest.approx(0.75)
    assert task.ratio == pytest.approx(0.75 / 8.0)
    assert [(p.phase, p.hours) for p in task.ai_phases] == [
        ("edit", 0.25),
        ("generate", 0.1),
        ("review", 0.5),
    ]
    assert result.ratio == pytest.approx(0.09375)
    assert result.log_valid is True
    assert result.kpi1_met is True


def test_ratio_is_pooled_over_measured_tasks_and_unmeasured_tasks_block_the_claim():
    events = span("a", "review", 0, 60) + span("b", "review", 60, 180)
    manual = baseline(("a", 4.0), ("b", 4.0), ("c", 10.0))
    result = report(events, manual)
    assert result.ai_hours == pytest.approx(3.0)
    assert result.manual_hours == pytest.approx(8.0)
    assert result.ratio == pytest.approx(0.375)
    assert result.unmeasured_tasks == ["c"]
    assert result.tasks[2].ratio is None
    assert result.kpi1_met is False


def test_equal_hours_do_not_meet_kpi1():
    result = report(span("a", "review", 0, 120), baseline(("a", 2.0)))
    assert result.ratio == 1.0
    assert result.kpi1_met is False


def test_overlapping_phases_are_counted_once():
    intervals, errors, still_open = pair_intervals(
        span("a", "review", 0, 60) + span("a", "generate", 30, 90, actor="syntera")
    )
    assert errors == [] and still_open == []
    assert union_hours(intervals) == pytest.approx(1.5)


@pytest.mark.parametrize(
    ("events", "fragment"),
    [
        ([make_event("a", "review", "stop", at=at(5))], "without a start"),
        (
            [make_event("a", "review", "start", at=at(0))] + span("a", "review", 5, 10),
            "started again",
        ),
        ([make_event("a", "review", "start", at=at(0))], "never stopped"),
        (span("ghost", "review", 0, 10), "task ghost has no manual baseline"),
    ],
)
def test_malformed_logs_fail_closed(events, fragment):
    result = report(events + span("a", "edit", 20, 30), baseline(("a", 8.0)))
    assert result.log_valid is False
    assert result.kpi1_met is False
    assert any(fragment in error for error in result.log_errors)


def test_log_lines_are_paired_by_time_not_file_order(tmp_path):
    log = tmp_path / "time_log.jsonl"
    stop, start = span("a", "review", 0, 30)[::-1]
    append_event(log, stop)
    append_event(log, start)
    parsed = read_log(log)
    result = kpi1_report(parsed, baseline(("a", 1.0)), "1" * 64)
    assert result.log_valid is True
    assert result.ai_hours == pytest.approx(0.5)
    assert len(result.log_sha256) == 64


def test_unreadable_lines_are_reported_with_line_numbers(tmp_path):
    log = tmp_path / "time_log.jsonl"
    append_event(log, span("a", "review", 0, 30)[0])
    with log.open("a", encoding="utf-8") as handle:
        handle.write("not json\n")
    append_event(log, span("a", "review", 0, 30)[1])
    parsed = read_log(log)
    assert len(parsed.events) == 2
    assert parsed.errors and parsed.errors[0].startswith("line 2:")
    assert kpi1_report(parsed, baseline(("a", 1.0)), "1" * 64).kpi1_met is False


def test_missing_log_is_empty_and_measures_nothing(tmp_path):
    parsed = read_log(tmp_path / "absent.jsonl")
    result = kpi1_report(parsed, baseline(("a", 1.0)), "1" * 64)
    assert parsed.events == [] and result.ratio is None and result.kpi1_met is False


def test_events_need_timezones_and_plain_labels():
    with pytest.raises(ValidationError):
        TimeEvent(task="a", phase="review", event="start", at=datetime(2026, 10, 15, 9))
    with pytest.raises(ValidationError):
        make_event("two words", "review", "start")
    with pytest.raises(ValidationError):
        make_event("a", "review", "pause")


def test_manual_baseline_rejects_duplicate_tasks():
    with pytest.raises(ValidationError, match="unique"):
        baseline(("a", 1.0), ("a", 2.0))


def test_example_manual_baseline_is_labelled_synthetic():
    manual, digest = load_manual_baseline(PROJECT_ROOT / "examples" / "manual_baseline.yaml")
    assert manual.synthetic_data is True
    totals = {task.task: sum(phase.hours for phase in task.phases) for task in manual.tasks}
    assert totals == {"one-line": 8.0, "three-lines": 18.0, "three-lines-fea": 29.0}
    assert all(phase.estimate for task in manual.tasks for phase in task.phases)
    assert len(digest) == 64
