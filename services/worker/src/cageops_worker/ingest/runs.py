"""Starting a run: the entry points the CLI (checkpoint 4) and a scheduler call.

A run is one invocation, identified by a run id that is stamped on every job it spawns and keys the
per-run row counts in Redis.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from cageops_scraper.errors import SourceBlocked
from cageops_worker.ingest.context import IngestContext
from cageops_worker.ingest.jobs import _enqueue, events_list_url
from cageops_worker.ingest.queue import EnqueueResult
from cageops_worker.ingest.wikipedia_jobs import _enqueue_wiki, year_list_url


class UnsupportedForSource(Exception):
    """The command doesn't apply to the configured source."""


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def ensure_not_blocked(ctx: IngestContext) -> None:
    """Raise SourceBlocked while the circuit breaker is open: never queue (or replay) work for a
    site that has blocked us."""
    state = ctx.fetcher.breaker.state()
    if state is not None:
        raise SourceBlocked(state.reason, state.url)


@dataclass(frozen=True)
class StartedRun:
    run_id: str
    discovery: EnqueueResult  # not enqueued means the same discovery job is already queued/failed


def months_before(today: date, months: int) -> date:
    """The same day `months` calendar months ago (clamped to the month's last day)."""
    index = today.year * 12 + today.month - 1 - months
    year, month = divmod(index, 12)
    month += 1
    for day in (today.day, 30, 29, 28):
        try:
            return date(year, month, day)
        except ValueError:
            continue
    raise AssertionError("unreachable")


def start_backfill(
    ctx: IngestContext, since: date, *, force: bool = False, run_id: str | None = None
) -> StartedRun:
    """Queue the discovery of every completed event from `since` to today."""
    ensure_not_blocked(ctx)
    run_id = run_id or new_run_id()
    if ctx.source.name == "wikipedia":  # one list page per year, newest year first
        year = ctx.today().year
        result = _enqueue_wiki(
            ctx, "wiki_discover_events", f"wiki-discover-{year}-since-{since}", run_id,
            url=year_list_url(ctx, year), year=year, since=since.isoformat(), force=force,
        )  # fmt: skip
        return StartedRun(run_id, result)
    result = _enqueue(
        ctx, "discover_events", f"discover-completed-p1-since-{since}", run_id,
        url=events_list_url(ctx, "completed", 1),
        kind="completed", page=1, since=since.isoformat(), force=force,
    )  # fmt: skip
    return StartedRun(run_id, result)


def start_upcoming(
    ctx: IngestContext, *, force: bool = False, run_id: str | None = None
) -> StartedRun:
    """Queue a read of the upcoming events list."""
    if ctx.source.name == "wikipedia":
        raise UnsupportedForSource("scrape-upcoming isn't supported for the wikipedia source")
    ensure_not_blocked(ctx)
    run_id = run_id or new_run_id()
    result = _enqueue(
        ctx, "discover_events", "discover-upcoming", run_id,
        url=events_list_url(ctx, "upcoming", 1), kind="upcoming", force=force,
    )  # fmt: skip
    return StartedRun(run_id, result)


def enqueue_backfill(
    ctx: IngestContext, since: date, *, force: bool = False, run_id: str | None = None
) -> str:
    """start_backfill, returning just the run id."""
    return start_backfill(ctx, since, force=force, run_id=run_id).run_id


def enqueue_upcoming(ctx: IngestContext, *, force: bool = False, run_id: str | None = None) -> str:
    """start_upcoming, returning just the run id."""
    return start_upcoming(ctx, force=force, run_id=run_id).run_id
