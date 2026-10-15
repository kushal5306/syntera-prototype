"""KPI 1 time log: record how long each phase of a benchmark task takes, and report the ratio.

The log is append-only JSON Lines. Engineers record review, correction and sign-off phases
from the CLI or the web dashboard; Syntera records its own compute time. Time for one task
is the union of its intervals, so a review that overlaps a regeneration is counted once.
A malformed log (a stop without a start, a phase started twice, a phase still running)
fails closed: the report lists every problem and never claims KPI 1.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import ValidationError

from syntera.schemas import (
    Kpi1Report,
    ManualBaseline,
    PhaseHours,
    TaskKpi1,
    TimeActor,
    TimeEvent,
)

TIME_LOG_FILE = "time_log.jsonl"
KPI1_FILE = "kpi1_report.json"


@dataclass(frozen=True)
class Interval:
    task: str
    phase: str
    actor: TimeActor
    start: datetime
    stop: datetime

    @property
    def hours(self) -> float:
        return (self.stop - self.start).total_seconds() / 3600


@dataclass(frozen=True)
class ParsedLog:
    events: list[TimeEvent]
    sha256: str
    errors: list[str]


def utc_now() -> datetime:
    return datetime.now(UTC)


def make_event(
    task: str,
    phase: str,
    event: str,
    actor: TimeActor = "engineer",
    note: str | None = None,
    at: datetime | None = None,
) -> TimeEvent:
    return TimeEvent(
        task=task, phase=phase, event=event, actor=actor, note=note, at=at or utc_now()
    )


def append_event(log_path: Path, event: TimeEvent) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(event.model_dump_json() + "\n")


def read_log(log_path: Path) -> ParsedLog:
    """Every valid event plus one error per unreadable line; a missing log is empty."""
    data = log_path.read_bytes() if log_path.is_file() else b""
    events: list[TimeEvent] = []
    errors: list[str] = []
    for number, line in enumerate(data.decode("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            events.append(TimeEvent.model_validate(json.loads(line)))
        except (json.JSONDecodeError, ValidationError) as error:
            detail = str(error).splitlines()[0]
            errors.append(f"line {number}: not a valid time event ({detail})")
    return ParsedLog(events=events, sha256=hashlib.sha256(data).hexdigest(), errors=errors)


def pair_intervals(events: list[TimeEvent]) -> tuple[list[Interval], list[str], list[TimeEvent]]:
    """Closed intervals, sequence errors, and the start events still open at the end."""
    ordered = sorted(enumerate(events), key=lambda item: (item[1].at, item[0]))
    open_starts: dict[tuple[str, str, str], TimeEvent] = {}
    intervals: list[Interval] = []
    errors: list[str] = []
    for _, event in ordered:
        key = (event.task, event.phase, event.actor)
        label = f"{event.task}/{event.phase} ({event.actor})"
        if event.event == "start":
            if key in open_starts:
                errors.append(f"{label} started again at {event.at.isoformat()} while running")
            else:
                open_starts[key] = event
            continue
        start = open_starts.pop(key, None)
        if start is None:
            errors.append(f"{label} stopped at {event.at.isoformat()} without a start")
            continue
        intervals.append(Interval(event.task, event.phase, event.actor, start.at, event.at))
    still_open = [open_starts[key] for key in sorted(open_starts)]
    return intervals, errors, still_open


def union_hours(intervals: list[Interval]) -> float:
    """Wall-clock hours covered by at least one interval (overlaps counted once)."""
    total = 0.0
    current_start: datetime | None = None
    current_stop: datetime | None = None
    for interval in sorted(intervals, key=lambda item: (item.start, item.stop)):
        if current_stop is None or interval.start > current_stop:
            if current_start is not None and current_stop is not None:
                total += (current_stop - current_start).total_seconds()
            current_start, current_stop = interval.start, interval.stop
        elif interval.stop > current_stop:
            current_stop = interval.stop
    if current_start is not None and current_stop is not None:
        total += (current_stop - current_start).total_seconds()
    return total / 3600


def phase_hours(intervals: list[Interval]) -> list[PhaseHours]:
    totals: dict[str, float] = defaultdict(float)
    for interval in intervals:
        totals[interval.phase] += interval.hours
    return [PhaseHours(phase=phase, hours=round(totals[phase], 4)) for phase in sorted(totals)]


def task_hours(events: list[TimeEvent]) -> dict[str, float]:
    """Closed AI hours per task, for live display; open phases are not counted."""
    intervals, _, _ = pair_intervals(events)
    by_task: dict[str, list[Interval]] = defaultdict(list)
    for interval in intervals:
        by_task[interval.task].append(interval)
    return {task: round(union_hours(by_task[task]), 4) for task in sorted(by_task)}


def load_manual_baseline(path: Path) -> tuple[ManualBaseline, str]:
    data = path.read_bytes()
    baseline = ManualBaseline.model_validate(yaml.safe_load(data.decode("utf-8")))
    return baseline, hashlib.sha256(data).hexdigest()


def kpi1_report(log: ParsedLog, baseline: ManualBaseline, baseline_sha256: str) -> Kpi1Report:
    intervals, sequence_errors, still_open = pair_intervals(log.events)
    errors = list(log.errors) + sequence_errors
    errors.extend(
        f"{event.task}/{event.phase} ({event.actor}) started at {event.at.isoformat()} "
        "and was never stopped"
        for event in still_open
    )
    known = {task.task for task in baseline.tasks}
    logged = sorted({event.task for event in log.events})
    errors.extend(f"task {task} has no manual baseline" for task in logged if task not in known)

    by_task: dict[str, list[Interval]] = defaultdict(list)
    for interval in intervals:
        by_task[interval.task].append(interval)

    rows: list[TaskKpi1] = []
    for task in baseline.tasks:
        manual = sum(phase.hours for phase in task.phases)
        ai = union_hours(by_task[task.task])
        measured = bool(by_task[task.task])
        rows.append(
            TaskKpi1(
                task=task.task,
                measured=measured,
                ai_hours=round(ai, 4),
                manual_hours=round(manual, 4),
                ratio=round(ai / manual, 6) if measured else None,
                manual_estimated=any(phase.estimate for phase in task.phases),
                ai_phases=phase_hours(by_task[task.task]),
                manual_phases=[
                    PhaseHours(phase=phase.phase, hours=phase.hours, estimate=phase.estimate)
                    for phase in task.phases
                ],
            )
        )
    measured_rows = [row for row in rows if row.measured]
    ai_total = sum(union_hours(by_task[row.task]) for row in measured_rows)
    manual_total = sum(row.manual_hours for row in measured_rows)
    ratio = round(ai_total / manual_total, 6) if measured_rows else None
    unmeasured = [row.task for row in rows if not row.measured]
    return Kpi1Report(
        baseline_title=baseline.title,
        baseline_synthetic=baseline.synthetic_data,
        baseline_source=baseline.source,
        baseline_sha256=baseline_sha256,
        log_sha256=log.sha256,
        events=len(log.events),
        tasks=rows,
        ai_hours=round(ai_total, 4),
        manual_hours=round(manual_total, 4),
        ratio=ratio,
        log_valid=not errors,
        log_errors=errors,
        unmeasured_tasks=unmeasured,
        kpi1_met=not errors and not unmeasured and ratio is not None and ratio < 1,
    )


def kpi1_from_files(log_path: Path, baseline_path: Path) -> Kpi1Report:
    baseline, digest = load_manual_baseline(baseline_path)
    return kpi1_report(read_log(log_path), baseline, digest)
