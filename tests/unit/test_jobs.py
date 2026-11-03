"""In-process background job queue (I1)."""

from __future__ import annotations

import json
import threading

import pytest

from syntera.jobs import InProcessQueue, JobError, JobRecord


def _queue(root, log=None, gate=None):
    log = [] if log is None else log

    def ok(payload, folder):
        if gate is not None:
            gate.wait(5)
        log.append(payload["n"])
        (folder / "out.txt").write_text(str(payload["n"]))
        return {"double": payload["n"] * 2}

    def broken(payload, folder):
        raise RuntimeError("solver crashed")

    return InProcessQueue(root, {"ok": ok, "broken": broken}), log


def test_jobs_run_in_submission_order_and_report_results(tmp_path):
    queue, log = _queue(tmp_path)
    records = [queue.submit("ok", {"n": n}) for n in range(5)]
    assert [r.id for r in records] == [f"job-00000{i}" for i in range(1, 6)]
    assert all(r.status == "queued" for r in records)
    for record in records:
        done = queue.wait(record.id, timeout=10)
        assert done.status == "succeeded"
        assert done.result == {"double": record.payload["n"] * 2}
    assert log == [0, 1, 2, 3, 4]
    assert (tmp_path / "job-000003" / "out.txt").read_text() == "2"
    queue.shutdown()


def test_failures_are_recorded_not_raised(tmp_path):
    queue, _ = _queue(tmp_path)
    failed = queue.wait(queue.submit("broken", {}).id, timeout=10)
    assert failed.status == "failed" and failed.error == "RuntimeError: solver crashed"
    with pytest.raises(JobError, match="unknown job kind"):
        queue.submit("nope", {})
    with pytest.raises(KeyError):
        queue.get("job-999999")
    queue.shutdown()


def test_status_is_visible_while_running_and_persists(tmp_path):
    gate = threading.Event()
    queue, _ = _queue(tmp_path, gate=gate)
    first = queue.submit("ok", {"n": 1})
    second = queue.submit("ok", {"n": 2})
    for _ in range(100):
        if queue.get(first.id).status == "running":
            break
        threading.Event().wait(0.01)
    assert queue.get(first.id).status == "running"
    assert queue.get(second.id).status == "queued"
    gate.set()
    queue.wait(second.id, timeout=10)
    queue.shutdown()
    saved = json.loads((tmp_path / f"{second.id}.json").read_text())
    assert saved["status"] == "succeeded"

    # a job left running by a crash is reported as interrupted after a restart
    stale = JobRecord(id="job-000003", kind="ok", status="running", payload={"n": 3})
    (tmp_path / "job-000003.json").write_text(stale.model_dump_json())
    restarted, _ = _queue(tmp_path)
    assert restarted.get("job-000003").status == "failed"
    assert "interrupted" in restarted.get("job-000003").error
    assert [r.id for r in restarted.list()] == ["job-000001", "job-000002", "job-000003"]
    assert restarted.submit("ok", {"n": 4}).id == "job-000004"
    restarted.shutdown()
