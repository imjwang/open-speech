"""Background jobs with progress (04 § Jobs): a clone being processed, voice candidates being designed. Each job runs
on the engine's loop, reports `job.progress` events (the bar draws them like a model download) and keeps its result
or its error for `GET /jobs/{id}`. Model downloads have their own jobs (local/manager.py); the route finds both."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from open_speech.api.ws_schema import JobProgress
from open_speech.errors import EngineError
from open_speech.ids import new_id
from open_speech.service.events import EventBus

log = logging.getLogger(__name__)
KEEP = 64  # finished jobs remembered


@dataclass
class Job:
    id: str
    kind: str
    pct: float = 0.0
    label: str = ""
    state: Literal["running", "done", "failed"] = "running"
    error: dict[str, Any] | None = None
    result: dict[str, Any] = field(default_factory=dict)
    task: asyncio.Task[Any] | None = None
    _last: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"job_id": self.id, "kind": self.kind, "pct": self.pct, "label": self.label, "state": self.state,
                "error": self.error, "result": self.result}


class Progress:
    """What a job's work calls to say how far it is."""

    def __init__(self, jobs: Jobs, job: Job) -> None:
        self._jobs = jobs
        self.job = job

    def __call__(self, pct: float, label: str | None = None, *, force: bool = False) -> None:
        self._jobs.progress(self.job, pct, label, force=force)


Work = Callable[[Progress], Awaitable[dict[str, Any]]]


class Jobs:
    def __init__(self, events: EventBus | None = None) -> None:
        self.events = events
        self._jobs: dict[str, Job] = {}

    def start(self, kind: str, work: Work, *, label: str = "") -> Job:
        job = Job(id=new_id("job"), kind=kind, label=label)
        self._jobs[job.id] = job
        self._trim()
        job.task = asyncio.get_running_loop().create_task(self._run(job, work), name=f"job-{kind}")
        return job

    async def _run(self, job: Job, work: Work) -> None:
        self.progress(job, 0.0, job.label, force=True)
        try:
            job.result = await work(Progress(self, job)) or {}
            job.state = "done"
            self.progress(job, 100.0, job.label, force=True)
        except asyncio.CancelledError:
            job.state = "failed"
            job.error = {"code": "cancelled", "detail": "Cancelled."}
            raise
        except EngineError as exc:
            job.state = "failed"
            job.error = exc.problem()
            self.progress(job, job.pct, exc.detail, force=True)
        except Exception as exc:  # noqa: BLE001 — a bug: logged, and the job says it failed
            log.exception("job %s failed", job.kind)
            job.state = "failed"
            job.error = {"code": "internal", "detail": str(exc)[:300]}
            self.progress(job, job.pct, job.label, force=True)

    def progress(self, job: Job, pct: float, label: str | None = None, *, force: bool = False) -> None:
        now = time.monotonic()
        if label is not None and label != job.label:
            force = True
        if not force and pct - job.pct < 1.0 and now - job._last < 0.25:
            return
        job.pct = round(max(job.pct if job.state == "running" else 0.0, min(100.0, pct)), 1)
        if label is not None:
            job.label = label
        job._last = now
        if self.events is not None:
            kind = job.kind if job.kind in ("install", "clone", "design") else None
            self.events.publish(JobProgress(job_id=job.id, pct=job.pct, label=job.label, kind=kind))  # type: ignore[arg-type]

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    async def wait(self, job_id: str, timeout: float | None = None) -> Job:
        job = self._jobs[job_id]
        if job.task is not None:
            await asyncio.wait({job.task}, timeout=timeout)
        return job

    def _trim(self) -> None:
        done = [j for j in self._jobs.values() if j.state != "running"]
        for j in done[: max(0, len(done) - KEEP)]:
            self._jobs.pop(j.id, None)

    async def close(self) -> None:
        tasks = [j.task for j in self._jobs.values() if j.task is not None and not j.task.done()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


__all__ = ["Job", "Jobs", "Progress"]
