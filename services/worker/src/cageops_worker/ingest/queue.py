"""Putting jobs on the queue exactly once (D-021).

Many fights point at the same fighter, so a backfill would enqueue "fetch fighter X" over and
over. Giving each job a deterministic id (fighter-<id>) isn't enough: RQ happily queues two jobs
with the same id. So we use a claim: a name card on a reserved table. Redis `SET key value NX`
puts the card down only if nobody else has, and it is one atomic step, so even two workers
enqueueing the same job in the same instant can't both win.

(RQ only allows letters, numbers, underscores and dashes in a job id, hence "fighter-<id>" and not
"fighter:<id>".)

Claim lifecycle:
- put down at enqueue, with an expiry as a safety net (a crashed worker mustn't hold an id forever);
- deleted when the job succeeds, so a later run can enqueue it again;
- made permanent when the job is dead-lettered, so a rerun doesn't pile onto a failure; replaying or
  purging it deletes the claim.

Dedupe is an optimisation, not what keeps the data correct: even if a duplicate slips through (the
claim expired during a very long backfill), the fetcher cache and the idempotent upserts make the
second run harmless.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from redis import Redis
from rq import Queue, Retry

from cageops_worker.ingest.backoff import backoff_schedule
from cageops_worker.ingest.config import IngestSettings

CLAIM_PREFIX = "ingest:claim:"
DEAD_LETTERED = "dead_lettered"


def claim_key(job_id: str) -> str:
    return f"{CLAIM_PREFIX}{job_id}"


def get_queue(redis: Redis, settings: IngestSettings) -> Queue:
    return Queue(settings.ingest_queue, connection=redis)


@dataclass(frozen=True)
class EnqueueResult:
    enqueued: bool
    reason: str  # "enqueued", "already_claimed" or "dead_lettered"


def enqueue_unique(
    queue: Queue,
    func: str,
    *,
    job_id: str,
    run_id: str,
    settings: IngestSettings,
    args: Sequence[Any] = (),
    kwargs: Mapping[str, Any] | None = None,
    meta: Mapping[str, Any] | None = None,
    rng: random.Random | None = None,
) -> EnqueueResult:
    """Enqueue `func` (a dotted path, so we don't import the job code here) unless a job with this
    id is already queued, running, retrying or dead-lettered."""
    redis = queue.connection
    won = redis.set(claim_key(job_id), run_id, nx=True, ex=settings.ingest_claim_ttl_s)
    if not won:
        state = claim_state(redis, job_id)
        return EnqueueResult(
            False, "dead_lettered" if state == DEAD_LETTERED else "already_claimed"
        )

    retry = None
    if settings.ingest_max_retries > 0:
        delays = backoff_schedule(
            settings.ingest_max_retries,
            settings.ingest_retry_base_s,
            settings.ingest_retry_cap_s,
            rng,
        )
        retry = Retry(max=settings.ingest_max_retries, interval=delays)
    try:
        queue.enqueue(
            func,
            args=tuple(args),
            kwargs=dict(kwargs or {}),
            job_id=job_id,
            retry=retry,
            job_timeout=settings.ingest_job_timeout_s,
            result_ttl=0,  # nothing reads a result; the job disappears when it succeeds
            failure_ttl=-1,  # a dead letter never expires on its own
            meta={"job_type": func.rsplit(".", 1)[-1], "run_id": run_id, **(meta or {})},
        )
    except BaseException:
        release_claim(redis, job_id)  # don't leave a card on a table nobody is sitting at
        raise
    return EnqueueResult(True, "enqueued")


def release_claim(redis: Redis, job_id: str) -> None:
    redis.delete(claim_key(job_id))


def keep_claim_for_dead_letter(redis: Redis, job_id: str) -> None:
    """The job failed for good: keep its claim with NO expiry, so reruns don't re-enqueue it."""
    redis.set(claim_key(job_id), DEAD_LETTERED)


def claim_state(redis: Redis, job_id: str) -> str | None:
    """None (no claim), "dead_lettered", or the run id that claimed it."""
    value = redis.get(claim_key(job_id))
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else value
