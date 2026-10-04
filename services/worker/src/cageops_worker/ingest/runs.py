"""Starting a run: the entry points the CLI (checkpoint 4) and a scheduler call.

A run is one invocation, identified by a run id that is stamped on every job it spawns and keys the
per-run row counts in Redis.
"""

from __future__ import annotations

import uuid
from datetime import date

from cageops_scraper.errors import SourceBlocked
from cageops_worker.ingest.context import IngestContext
from cageops_worker.ingest.jobs import _enqueue


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def _refuse_if_blocked(ctx: IngestContext) -> None:
    state = ctx.fetcher.breaker.state()
    if state is not None:  # never queue work for a site that has blocked us
        raise SourceBlocked(state.reason, state.url)


def enqueue_backfill(
    ctx: IngestContext, since: date, *, force: bool = False, run_id: str | None = None
) -> str:
    """Queue the discovery of every completed event from `since` to today. Returns the run id."""
    _refuse_if_blocked(ctx)
    run_id = run_id or new_run_id()
    _enqueue(
        ctx, "discover_events", f"discover-completed-p1-since-{since}", run_id,
        kind="completed", page=1, since=since.isoformat(), force=force,
    )  # fmt: skip
    return run_id


def enqueue_upcoming(ctx: IngestContext, *, force: bool = False, run_id: str | None = None) -> str:
    """Queue a read of the upcoming events list. Returns the run id."""
    _refuse_if_blocked(ctx)
    run_id = run_id or new_run_id()
    _enqueue(ctx, "discover_events", "discover-upcoming", run_id, kind="upcoming", force=force)
    return run_id
