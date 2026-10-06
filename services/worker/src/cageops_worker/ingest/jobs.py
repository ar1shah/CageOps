"""The four ingestion jobs, and the wrapper every one of them runs inside.

    discover_events  ->  fetch_event  ->  fetch_fight   (one per bout that has a result)
                                      ->  fetch_fighter (one per fighter, deduped by claim)

Each job takes only plain arguments (RQ stores them in Redis) and gets its database, Redis and
fetcher from the per-process context. Each is idempotent: run it twice and the database ends up
identical (see upsert.py), so a retry or a replay can never double anything.
"""

from __future__ import annotations

import functools
import logging
import time
from datetime import UTC, date, datetime
from typing import Any

import sqlalchemy as sa
from rq import get_current_job

from cageops_common.logs import log_context
from cageops_scraper.anomalies import report_anomalies
from cageops_scraper.errors import NotFound
from cageops_scraper.parsers.common import ufcstats_id
from cageops_scraper.parsers.event import parse_event
from cageops_scraper.parsers.events import parse_events_list
from cageops_scraper.parsers.fight import parse_fight
from cageops_scraper.parsers.fighter import parse_fighter
from cageops_scraper.parsers.models import EventBout
from cageops_scraper.sources import PageKind
from cageops_worker.ingest import store
from cageops_worker.ingest.context import IngestContext, get_context
from cageops_worker.ingest.mapping import (
    map_event,
    map_fight,
    map_fighter,
    map_scheduled_bout,
    split_bouts,
)
from cageops_worker.ingest.metrics import (
    DEAD_LETTERS_TOTAL,
    ENQUEUE_TOTAL,
    JOB_SECONDS,
    JOBS_TOTAL,
    ROWS_TOTAL,
)
from cageops_worker.ingest.policy import RETRIES_EXHAUSTED, classify
from cageops_worker.ingest.queue import (
    EnqueueResult,
    enqueue_unique,
    keep_claim_for_dead_letter,
    release_claim,
)
from cageops_worker.ingest.upsert import UpsertCounts, record_counts

log = logging.getLogger(__name__)

JOBS = "cageops_worker.ingest.jobs"
MAX_DISCOVER_PAGES = 80  # ~790 events at 25 per page is 32 pages; this stops a runaway chain
STALE_SCHEDULED_DAYS = 7


# -- the wrapper -----------------------------------------------------------------------


def tracked(fn):
    """Run a job inside the standard envelope: log context, timing, metrics, failure handling.

    On failure it classifies the error (policy.py). A permanent error zeroes the job's retries so RQ
    sends it straight to the failed registry; a retryable one is left for RQ to reschedule. Either
    way, once a job is out of road it is stamped with why (job.meta) and keeps its claim.
    """
    job_type = fn.__name__

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        ctx = get_context()
        job = get_current_job()
        url = (job.meta.get("url") if job else None) or next(
            (a for a in args if isinstance(a, str) and a.startswith("http")), None
        )
        with log_context(job_id=job.id if job else None, url=url):
            started = time.perf_counter()
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                _on_failure(ctx, job, job_type, exc, url, time.perf_counter() - started)
                raise
            JOB_SECONDS.labels(job_type).observe(time.perf_counter() - started)
            JOBS_TOTAL.labels(job_type, "success").inc()
            if job is not None:
                release_claim(ctx.redis, job.id)  # a later run may enqueue this job again
            log.info("job finished", extra={"job_type": job_type, "result": result})
            return result

    return wrapper


def _on_failure(ctx: IngestContext, job, job_type: str, exc: Exception, url, elapsed: float):
    JOB_SECONDS.labels(job_type).observe(elapsed)
    disposition = classify(exc)
    retries_left = job.retries_left if job is not None else None
    if not disposition.retry and job is not None:
        job.retries_left = 0  # asking again can't help: skip the retries, go to the DLQ now
    if disposition.retry and retries_left:
        JOBS_TOTAL.labels(job_type, "retry").inc()
        log.warning(
            "job failed, will retry",
            extra={
                "reason": disposition.reason,
                "error_class": type(exc).__name__,
                "error": str(exc)[:300],
                "retries_left": retries_left,
            },  # fmt: skip
        )
        return

    reason = disposition.reason if not disposition.retry else RETRIES_EXHAUSTED
    JOBS_TOTAL.labels(job_type, "dead_letter").inc()
    DEAD_LETTERS_TOTAL.labels(job_type, reason).inc()
    max_retries = job.meta.get("max_retries", 0) if job is not None else 0
    attempts = 1 if not retries_left and not max_retries else max_retries - (retries_left or 0) + 1
    if job is not None:
        job.meta.update(
            reason=reason,
            last_reason=disposition.reason,
            error_class=type(exc).__name__,
            error=str(exc)[:500],
            attempts=attempts,
            failed_at=datetime.now(UTC).isoformat(),
        )
        job.save_meta()
        keep_claim_for_dead_letter(ctx.redis, job.id)
    log.error(
        "job dead-lettered",
        extra={"job_type": job_type, "reason": reason, "last_reason": disposition.reason,
               "error_class": type(exc).__name__, "error": str(exc)[:300], "attempts": attempts},
    )  # fmt: skip


# -- helpers ---------------------------------------------------------------------------


def _enqueue(
    ctx: IngestContext, func: str, job_id: str, run_id: str, *, url: str | None = None, **kwargs
) -> EnqueueResult:
    result = enqueue_unique(
        ctx.queue,
        f"{JOBS}.{func}",
        job_id=job_id,
        run_id=run_id,
        settings=ctx.settings,
        kwargs={"run_id": run_id, **kwargs},  # every job reports its row counts under the run
        meta={"url": url} if url else None,
    )
    ENQUEUE_TOTAL.labels(func, result.reason).inc()
    return result


def _enqueue_event(ctx, run_id, url, event_date: date, force) -> EnqueueResult:
    return _enqueue(
        ctx, "fetch_event", f"event-{ufcstats_id(url)}", run_id, url=url,
        event_url=url, event_date=event_date.isoformat(), force=force,
    )  # fmt: skip


def _record(ctx: IngestContext, run_id: str, table: str, counts: UpsertCounts) -> None:
    record_counts(ctx.redis, run_id, table, counts, ctx.settings.ingest_run_stats_ttl_s)
    for action, n in counts.as_dict().items():
        if n:
            ROWS_TOTAL.labels(table, action).inc(n)


def events_list_url(ctx: IngestContext, kind: str, page: int) -> str:
    base = f"{ctx.source.base_url}/statistics/events/{kind}"
    return base if page == 1 else f"{base}?page={page}"


# -- discover_events -------------------------------------------------------------------


@tracked
def discover_events(
    kind: str, run_id: str, page: int = 1, since: str | None = None, force: bool = False
) -> dict[str, Any]:
    """Read one page of the events list and enqueue what it points to.

    completed: every event dated from `since` to today; and, if this page still reaches back to
        `since`, the next page too (the list is newest first, 25 per page).
    upcoming: every listed event, plus a re-check of any event we hold scheduled bouts for that the
        list no longer shows (re-check the page; never guess that a card was cancelled).
    """
    ctx = get_context()
    if kind not in ("completed", "upcoming"):
        raise ValueError(f"unknown events list: {kind!r}")
    url = events_list_url(ctx, kind, page)
    page_kind = PageKind.EVENTS_COMPLETED if kind == "completed" else PageKind.EVENTS_UPCOMING
    fetched = ctx.fetcher.fetch(url, page_kind, force=force)
    rows = parse_events_list(fetched.html, url)
    today = ctx.today()
    summary: dict[str, Any] = {"kind": kind, "page": page, "rows": len(rows), "events": 0}

    if kind == "completed":
        since_date = date.fromisoformat(since or "")
        for row in rows:
            # the first row of the "completed" list is the next upcoming event: it is in the future
            if since_date <= row.event_date <= today:
                _enqueue_event(ctx, run_id, row.url, row.event_date, force)
                summary["events"] += 1
        if rows and min(r.event_date for r in rows) >= since_date:  # more history to go
            if page >= MAX_DISCOVER_PAGES:
                report_anomalies(ctx.source.name, [f"discover_page_limit:page={page}"])
            else:
                _enqueue(
                    ctx, "discover_events", f"discover-completed-p{page + 1}-since-{since}",
                    run_id, url=events_list_url(ctx, kind, page + 1),
                    kind=kind, page=page + 1, since=since, force=force,
                )  # fmt: skip
                summary["next_page"] = page + 1
        return summary

    for row in rows:
        _enqueue_event(ctx, run_id, row.url, row.event_date, force)
        summary["events"] += 1
    listed = {r.ufcstats_id for r in rows}
    with ctx.engine.connect() as conn:
        held = conn.execute(
            sa.text(
                "SELECT e.ufcstats_id, e.event_date FROM events e WHERE e.event_date >= :today"
                " AND e.ufcstats_id IS NOT NULL"
                " AND EXISTS (SELECT 1 FROM fights f WHERE f.event_id = e.id"
                " AND f.status = 'scheduled')"
            ),
            {"today": today},
        ).all()
        stale = conn.execute(
            sa.text(
                "SELECT count(*) FROM fights f JOIN events e ON e.id = f.event_id"
                " WHERE f.status = 'scheduled' AND e.event_date < :cutoff"
            ),
            {"cutoff": today.fromordinal(today.toordinal() - STALE_SCHEDULED_DAYS)},
        ).scalar_one()
    for ufcstats, event_date in held:
        if ufcstats not in listed:
            _enqueue_event(
                ctx, run_id, f"{ctx.source.base_url}/event-details/{ufcstats}", event_date, True
            )  # force: the cached page can't tell us the card was dropped
            summary["rechecked"] = summary.get("rechecked", 0) + 1
    if stale:
        report_anomalies(ctx.source.name, [f"stale_scheduled_bout:count={stale}"])
    return summary


# -- fetch_event -----------------------------------------------------------------------


@tracked
def fetch_event(
    event_url: str, event_date: str, run_id: str, force: bool = False
) -> dict[str, Any]:
    """Fetch an event page and, in ONE transaction, write the event, its scheduled bouts and the
    cancellations; then enqueue a fight job per bout that has a result and a fighter job per
    fighter. The date written is the EVENT PAGE's, never the list's."""
    ctx = get_context()
    expected_date = date.fromisoformat(event_date)
    try:
        fetched = ctx.fetcher.fetch(
            event_url, PageKind.EVENT, event_date=expected_date, force=force
        )
    except NotFound:
        return _event_gone(ctx, event_url, run_id)
    event = parse_event(fetched.html, event_url)
    played, unplayed = split_bouts(event)
    scheduled = [map_scheduled_bout(b, event.ufcstats_id) for b in unplayed]
    anomalies = [a for s in scheduled for a in s.anomalies]
    if event.event_date != expected_date:
        anomalies.append(f"event_date_mismatch:list={expected_date}:page={event.event_date}")

    with ctx.engine.begin() as conn:
        event_counts, event_db_id = store.write_event(conn, map_event(event))
        # name-only fighter rows for the card, counted here (write_scheduled_bouts would find them)
        stub_counts, _ = store.ensure_fighters(conn, [f for s in scheduled for f in s.fighters])
        scheduled_counts = store.write_scheduled_bouts(conn, event.ufcstats_id, scheduled)
        reconciled = store.reconcile_event_bouts(
            conn, event_db_id, [b.fight_id for b in event.bouts]
        )
    anomalies += reconciled.anomalies
    report_anomalies(ctx.source.name, anomalies)
    _record(ctx, run_id, "events", event_counts)
    _record(ctx, run_id, "fights_scheduled", scheduled_counts)
    _record(ctx, run_id, "fighters", stub_counts)

    for bout in played:  # only fights with a result have a stats page worth fetching
        _enqueue(
            ctx, "fetch_fight", f"fight-{bout.fight_id}", run_id, url=bout.fight_url,
            fight_url=bout.fight_url, event_ufcstats_id=event.ufcstats_id,
            event_date=event.event_date.isoformat(), bout=bout.model_dump(mode="json"), force=force,
        )  # fmt: skip
    fighters = {f.ufcstats_id for b in event.bouts for f in b.fighters}
    for fighter_id in sorted(fighters):  # claims make a fighter on several cards one job
        url = f"{ctx.source.base_url}/fighter-details/{fighter_id}"
        _enqueue(ctx, "fetch_fighter", f"fighter-{fighter_id}", run_id, url=url,
                 fighter_url=url, force=force)  # fmt: skip
    return {
        "event": event.ufcstats_id,
        "bouts": len(event.bouts),
        "fights_enqueued": len(played),
        "scheduled": len(scheduled),
        "cancelled": len(reconciled.cancelled),
        "fighters": len(fighters),
    }


def _event_gone(ctx: IngestContext, event_url: str, run_id: str) -> dict[str, Any]:
    """The event page 404s. If we know the event, its page was removed (the card is off): cancel
    its scheduled bouts. If we don't, it is just a bad link, so let the 404 fail the job."""
    with ctx.engine.begin() as conn:
        event_db_id = store.event_id(conn, ufcstats_id(event_url))
        if event_db_id is None:
            raise NotFound(event_url)
        cancelled = store.cancel_scheduled_for_event(conn, event_db_id)
    report_anomalies(ctx.source.name, [f"event_page_gone:event={ufcstats_id(event_url)}"])
    return {"event": ufcstats_id(event_url), "gone": True, "cancelled": len(cancelled)}


# -- fetch_fight -----------------------------------------------------------------------


@tracked
def fetch_fight(
    fight_url: str,
    event_ufcstats_id: str,
    event_date: str,
    bout: dict[str, Any] | None,
    run_id: str,
    force: bool = False,
) -> dict[str, Any]:
    """Fetch a fight page and write the fight with its totals and per-round stats.

    `bout` is the event page's row for this fight (it gives the plain division name and lets us
    cross-check the result); None works too, e.g. for a manual replay, and falls back to the title.

    `event_date` only decides how long the cached page stays fresh; the fight table has no date
    column at all, and the fight's date is its event row's, written by fetch_event.
    """
    ctx = get_context()
    fetched = ctx.fetcher.fetch(
        fight_url, PageKind.FIGHT, event_date=date.fromisoformat(event_date), force=force
    )
    page = parse_fight(fetched.html, fight_url)
    mapped = map_fight(page, EventBout.model_validate(bout) if bout else None)
    anomalies = list(mapped.anomalies)
    if page.event_id != event_ufcstats_id:
        anomalies.append(f"fight_event_mismatch:expected={event_ufcstats_id}:page={page.event_id}")
    with ctx.engine.begin() as conn:
        written = store.write_completed_fight(conn, mapped)
    anomalies += written.anomalies
    report_anomalies(ctx.source.name, anomalies)
    _record(ctx, run_id, "fights", written.fight)
    _record(
        ctx, run_id, "fighters", written.fighters
    )  # name-only rows; bios come from fighter jobs
    _record(ctx, run_id, "fight_totals", written.totals)
    _record(ctx, run_id, "fight_round_stats", written.rounds)
    return {
        "fight": page.fight_id,
        "was": written.previous_status,
        "fights": written.fight.as_dict(),
        "fight_totals": written.totals.as_dict(),
        "fight_round_stats": written.rounds.as_dict(),
        "stat_rows_deleted": written.stale_stat_rows_deleted,
    }


# -- fetch_fighter ---------------------------------------------------------------------


@tracked
def fetch_fighter(fighter_url: str, run_id: str, force: bool = False) -> dict[str, Any]:
    """Fetch a fighter page and fill in their bio (height, reach, stance, date of birth)."""
    ctx = get_context()
    fetched = ctx.fetcher.fetch(fighter_url, PageKind.FIGHTER, force=force)
    row = map_fighter(parse_fighter(fetched.html, fighter_url))
    with ctx.engine.begin() as conn:
        counts = store.write_fighter_bio(conn, row)
    _record(ctx, run_id, "fighters", counts)
    return {"fighter": row["ufcstats_id"], "fighters": counts.as_dict()}
