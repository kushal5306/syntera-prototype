"""Background jobs: routing, FEA and optimisation off the request path (roadmap I1).

A :class:`JobQueue` accepts typed jobs and reports their status. The first
implementation, :class:`InProcessQueue`, runs one job at a time on a worker thread in
submission order and keeps every job record as JSON on disk, so status survives a
restart. An external queue (a broker and separate workers) can implement the same
protocol later without changing callers.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

JobStatus = Literal["queued", "running", "succeeded", "failed"]
Handler = Callable[[dict[str, Any], Path], dict[str, Any]]


class JobRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^job-[0-9]{6}$")
    kind: str
    status: JobStatus
    payload: dict[str, Any]
    result: dict[str, Any] | None = None
    error: str | None = None


class JobError(ValueError):
    """A job request that cannot be queued."""


class JobQueue(Protocol):
    def submit(self, kind: str, payload: dict[str, Any]) -> JobRecord: ...

    def get(self, job_id: str) -> JobRecord: ...

    def list(self) -> list[JobRecord]: ...


class InProcessQueue:
    """One worker thread; jobs run in submission order; records persist under ``root``."""

    def __init__(self, root: Path, handlers: dict[str, Handler]):
        self.root = root
        self.handlers = dict(handlers)
        self._lock = threading.Lock()
        self._records: dict[str, JobRecord] = {}
        self._futures: dict[str, Future] = {}
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="syntera-job")
        root.mkdir(parents=True, exist_ok=True)
        for path in sorted(root.glob("job-*.json")):
            record = JobRecord.model_validate_json(path.read_text(encoding="utf-8"))
            if record.status in {"queued", "running"}:  # interrupted by a restart
                record = record.model_copy(
                    update={"status": "failed", "error": "interrupted by a server restart"}
                )
                self._save(record)
            self._records[record.id] = record

    def _save(self, record: JobRecord) -> None:
        path = self.root / f"{record.id}.json"
        path.write_text(json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True)
                        + "\n", encoding="utf-8")  # fmt: skip

    def _update(self, job_id: str, **changes: Any) -> None:
        with self._lock:
            record = self._records[job_id].model_copy(update=changes)
            self._records[job_id] = record
            self._save(record)

    def submit(self, kind: str, payload: dict[str, Any]) -> JobRecord:
        if kind not in self.handlers:
            raise JobError(f"unknown job kind {kind}; kinds: {sorted(self.handlers)}")
        with self._lock:
            number = 1 + max((int(i.removeprefix("job-")) for i in self._records), default=0)
            record = JobRecord(id=f"job-{number:06d}", kind=kind, status="queued",
                               payload=payload)  # fmt: skip
            self._records[record.id] = record
            self._save(record)
        self._futures[record.id] = self._executor.submit(self._run, record.id)
        return record

    def _run(self, job_id: str) -> None:
        record = self._records[job_id]
        self._update(job_id, status="running")
        folder = self.root / job_id
        folder.mkdir(parents=True, exist_ok=True)
        try:
            result = self.handlers[record.kind](record.payload, folder)
        except Exception as error:  # noqa: BLE001 - every failure is reported on the job
            self._update(job_id, status="failed", error=f"{type(error).__name__}: {error}")
        else:
            self._update(job_id, status="succeeded", result=result)

    def get(self, job_id: str) -> JobRecord:
        with self._lock:
            if job_id not in self._records:
                raise KeyError(job_id)
            return self._records[job_id]

    def list(self) -> list[JobRecord]:
        with self._lock:
            return [self._records[key] for key in sorted(self._records)]

    def wait(self, job_id: str, timeout: float | None = None) -> JobRecord:
        """Block until the job has finished (for tests and command-line use)."""
        future = self._futures.get(job_id)
        if future is not None:
            future.result(timeout=timeout)
        return self.get(job_id)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=True)
