"""The dead-letter queue: inspect and replay jobs that failed for good (D-020).

RQ's failed registry already keeps what we need (the function, arguments, traceback and our own
`meta`), so it IS the DLQ; this module is the thin layer for working with it:
- list_failed / inspect: what failed, why, how many attempts, and the traceback;
- replay: send a dead letter around again with a FRESH retry budget (RQ's own requeue would keep
  retries_left at 0, so a replayed job would never retry);
- purge: give up on one for good.

Every dead-lettering is also a structured log line, which is the audit trail after a replay or purge
removes the entry.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from redis import Redis
from rq import Queue
from rq.exceptions import NoSuchJobError
from rq.job import Job
from rq.registry import FailedJobRegistry

from cageops_worker.ingest.config import IngestSettings
from cageops_worker.ingest.queue import (
    EnqueueResult,
    enqueue_unique,
    keep_claim_for_dead_letter,
    release_claim,
)
from cageops_worker.ingest.runs import new_run_id


class DeadLetterNotFound(LookupError):
    pass


@dataclass(frozen=True)
class DeadLetter:
    job_id: str
    job_type: str
    url: str | None
    reason: str  # what to filter by: not_found, parse_error, retries_exhausted, source_blocked, ...
    last_reason: str | None  # for retries_exhausted: what kept failing (fetch_failed, timeout, ...)
    error_class: str | None
    error: str | None
    attempts: int | None
    run_id: str | None
    failed_at: str | None


@dataclass(frozen=True)
class DeadLetterDetail:
    letter: DeadLetter
    func_name: str
    args: tuple
    kwargs: dict[str, Any]
    traceback: str


@dataclass
class ReplayReport:
    replayed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # already queued again, so left alone


def _registry(queue: Queue) -> FailedJobRegistry:
    return FailedJobRegistry(queue=queue)


def _letter(job: Job) -> DeadLetter:
    meta = job.meta
    reason = meta.get("reason")
    if reason is None:  # a failure our wrapper never saw, e.g. the worker process was killed
        trace = _traceback(job)
        reason = "worker_lost" if "AbandonedJobError" in trace else "unknown"
    return DeadLetter(
        job_id=job.id,
        job_type=meta.get("job_type", job.func_name.rsplit(".", 1)[-1]),
        url=meta.get("url"),
        reason=reason,
        last_reason=meta.get("last_reason"),
        error_class=meta.get("error_class"),
        error=meta.get("error"),
        attempts=meta.get("attempts"),
        run_id=meta.get("run_id"),
        failed_at=meta.get("failed_at"),
    )


def _traceback(job: Job) -> str:
    result = job.latest_result()
    return (result.exc_string if result is not None else None) or job.exc_info or ""


def list_failed(queue: Queue, reason: str | None = None) -> list[DeadLetter]:
    """Every dead letter, oldest failure first, optionally only those with one `reason`."""
    ids = _registry(queue).get_job_ids()
    jobs = [j for j in Job.fetch_many(ids, connection=queue.connection) if j is not None]
    letters = [_letter(j) for j in jobs]
    if reason is not None:
        letters = [letter for letter in letters if letter.reason == reason]
    return sorted(letters, key=lambda letter: letter.failed_at or "")


def summary(queue: Queue) -> Counter[str]:
    """{'not_found': 31, 'retries_exhausted': 2, ...}"""
    return Counter(letter.reason for letter in list_failed(queue))


def inspect(queue: Queue, job_id: str) -> DeadLetterDetail:
    job = _fetch_failed(queue, job_id)
    return DeadLetterDetail(
        letter=_letter(job),
        func_name=job.func_name,
        args=tuple(job.args),
        kwargs=dict(job.kwargs),
        traceback=_traceback(job),
    )


def _fetch_failed(queue: Queue, job_id: str) -> Job:
    if job_id not in _registry(queue).get_job_ids():
        raise DeadLetterNotFound(f"{job_id} is not in the dead-letter queue")
    try:
        return Job.fetch(job_id, connection=queue.connection)
    except NoSuchJobError as exc:
        raise DeadLetterNotFound(f"{job_id} is listed but its data is gone") from exc


def replay(
    queue: Queue, settings: IngestSettings, job_id: str, *, run_id: str | None = None
) -> EnqueueResult:
    """Send one dead letter around again with a fresh retry budget.

    Removes it from the DLQ and clears its claim, then enqueues it as a new job. If that enqueue
    fails the dead letter is put back, so a replay can never lose one.
    """
    redis: Redis = queue.connection
    job = _fetch_failed(queue, job_id)
    run_id = run_id or f"replay-{new_run_id()}"
    kwargs = dict(job.kwargs)
    if "run_id" in kwargs:
        kwargs["run_id"] = run_id  # this attempt's row counts belong to the replay
    meta: dict[str, Any] = {
        "replayed_from_reason": job.meta.get("reason"),
        "replayed_at": datetime.now(UTC).isoformat(),
    }
    if job.meta.get("url"):
        meta["url"] = job.meta["url"]

    _registry(queue).remove(job_id)  # out of the DLQ, but keep the job data until the new one lands
    release_claim(redis, job_id)
    try:
        return enqueue_unique(
            queue,
            job.func_name,
            job_id=job_id,
            run_id=run_id,
            settings=settings,
            args=tuple(job.args),
            kwargs=kwargs,
            meta=meta,
        )
    except BaseException:
        _registry(queue).add(job, ttl=-1)  # put it back
        keep_claim_for_dead_letter(redis, job_id)
        raise


def replay_all(
    queue: Queue,
    settings: IngestSettings,
    reason: str | None = None,
    *,
    on_each: Callable[[DeadLetter, EnqueueResult], None] | None = None,
) -> ReplayReport:
    """Replay every dead letter (or only those with `reason`) under one new run id."""
    report = ReplayReport()
    run_id = f"replay-{new_run_id()}"
    for letter in list_failed(queue, reason):
        result = replay(queue, settings, letter.job_id, run_id=run_id)
        (report.replayed if result.enqueued else report.skipped).append(letter.job_id)
        if on_each:
            on_each(letter, result)
    return report


def purge(queue: Queue, job_id: str) -> bool:
    """Give up on one dead letter: delete it and its claim. True if it was there."""
    if job_id not in _registry(queue).get_job_ids():
        return False
    _registry(queue).remove(job_id, delete_job=True)
    release_claim(queue.connection, job_id)
    return True
