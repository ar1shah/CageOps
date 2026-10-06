"""The Wikipedia ingestion jobs (D-028, D-029). Same envelope as the ufcstats jobs: `tracked`,
claims, retries, dead-letter queue, per-run row counts.

    wiki_discover_events  ->  wiki_fetch_event        (one per event article)

An event article holds every bout's result, so there is no fight or fighter job: one request per
event. Each job is idempotent, so a retry or a replay can never double anything.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from redis import Redis

from cageops_scraper.anomalies import report_anomalies
from cageops_scraper.parsers.wikipedia_event import parse_event
from cageops_scraper.parsers.wikipedia_events import parse_year_list
from cageops_scraper.sources import PageKind
from cageops_worker.ingest.context import IngestContext, get_context
from cageops_worker.ingest.jobs import _record, tracked
from cageops_worker.ingest.mapping_wikipedia import map_bout
from cageops_worker.ingest.metrics import ENQUEUE_TOTAL
from cageops_worker.ingest.queue import EnqueueResult, enqueue_unique
from cageops_worker.ingest.store_wikipedia import write_wikipedia_event

log = logging.getLogger(__name__)

WIKI_JOBS = "cageops_worker.ingest.wikipedia_jobs"


def year_list_url(ctx: IngestContext, year: int) -> str:
    return f"{ctx.source.base_url}/wiki/{year}_in_UFC"


def event_url(ctx: IngestContext, title: str) -> str:
    return f"{ctx.source.base_url}/wiki/{title}"


def _enqueue_wiki(
    ctx: IngestContext, func: str, job_id: str, run_id: str, *, url: str, **kwargs
) -> EnqueueResult:
    result = enqueue_unique(
        ctx.queue,
        f"{WIKI_JOBS}.{func}",
        job_id=job_id,
        run_id=run_id,
        settings=ctx.settings,
        kwargs={"run_id": run_id, **kwargs},
        meta={"url": url},
    )
    ENQUEUE_TOTAL.labels(func, result.reason).inc()
    return result


# -- what a run saw, beyond row counts ---------------------------------------------------------
# Kept apart from the integer counters in `ingest:run:<id>` so those stay plain numbers.


def record_detail(ctx: IngestContext, run_id: str, kind: str, values: dict[str, Any]) -> None:
    if not values:
        return
    key = f"ingest:run:{run_id}:{kind}"
    with ctx.redis.pipeline() as pipe:
        pipe.hset(key, mapping={str(k): str(v) for k, v in values.items()})
        pipe.expire(key, ctx.settings.ingest_run_stats_ttl_s)
        pipe.execute()


def read_details(redis: Redis, run_id: str, kind: str) -> dict[str, str]:
    raw = redis.hgetall(f"ingest:run:{run_id}:{kind}")
    return {_text(k): _text(v) for k, v in sorted(raw.items(), key=lambda kv: _text(kv[0]))}


def _text(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value


# -- wiki_discover_events --------------------------------------------------------------------


@tracked
def wiki_discover_events(run_id: str, year: int, since: str, force: bool = False) -> dict[str, Any]:
    """Read one "{year} in UFC" page and enqueue an event job for every past event dated from
    `since` to today. If `since` reaches into an earlier year, queue that year's page too."""
    ctx = get_context()
    url = year_list_url(ctx, year)
    fetched = ctx.fetcher.fetch(url, PageKind.EVENTS_COMPLETED, force=force)
    refs = parse_year_list(fetched.html, url)
    since_date, today = date.fromisoformat(since), ctx.today()
    summary: dict[str, Any] = {"year": year, "rows": len(refs), "events": 0, "without_article": 0}
    for ref in refs:
        if not since_date <= ref.event_date <= today:
            continue
        if ref.title is None:
            summary["without_article"] += 1
            report_anomalies(ctx.source.name, [f"event_without_article:{ref.name}"])
            continue
        _enqueue_wiki(
            ctx, "wiki_fetch_event", f"wiki-event-{ref.title}", run_id,
            url=event_url(ctx, ref.title), title=ref.title,
            event_date=ref.event_date.isoformat(), force=force,
        )  # fmt: skip
        summary["events"] += 1
    if since_date.year < year:
        _enqueue_wiki(
            ctx, "wiki_discover_events", f"wiki-discover-{year - 1}-since-{since}", run_id,
            url=year_list_url(ctx, year - 1), year=year - 1, since=since, force=force,
        )  # fmt: skip
        summary["next_year"] = year - 1
    return summary


# -- wiki_fetch_event -------------------------------------------------------------------------


@tracked
def wiki_fetch_event(
    title: str, event_date: str, run_id: str, force: bool = False
) -> dict[str, Any]:
    """Fetch one event article and, in ONE transaction, write its event, any new fighters and
    every bout. A name that needs a person's review (an ambiguous or suspect-duplicate fighter)
    fails the whole job into the dead-letter queue and writes nothing."""
    ctx = get_context()
    url = event_url(ctx, title)
    expected = date.fromisoformat(event_date)
    fetched = ctx.fetcher.fetch(url, PageKind.EVENT, event_date=expected, force=force)
    page = parse_event(fetched.html, url)
    bouts = [map_bout(b) for b in page.bouts]
    anomalies = list(page.anomalies)
    if page.event_date != expected:
        anomalies.append(f"event_date_mismatch:list={expected}:page={page.event_date}")

    with ctx.engine.begin() as conn:
        written = write_wikipedia_event(conn, page, bouts)
    anomalies += written.anomalies
    report_anomalies(ctx.source.name, anomalies)
    _record(ctx, run_id, "events", written.event)
    _record(ctx, run_id, "fights", written.fights)
    _record(ctx, run_id, "fighters", written.fighters)
    # Which revision of which article these results came from (for the request log), and every
    # fighter we had to create, with the link they came from (for review).
    record_detail(ctx, run_id, "revisions", {page.canonical_title: page.revision_id})
    record_detail(
        ctx,
        run_id,
        "stubs",
        {(s["link_title"] or s["name"]): s["name"] for s in written.stubs},
    )
    log.info(
        "wikipedia event written",
        extra={"article_id": page.article_id, "revision_id": page.revision_id,
               "stubs": [s["link_title"] or s["name"] for s in written.stubs]},
    )  # fmt: skip
    return {
        "event": page.canonical_title,
        "article_id": page.article_id,
        "revision_id": page.revision_id,
        "bouts": len(bouts),
        "already_covered": written.already_covered,
        "skipped": written.skipped,
        "stubs": len(written.stubs),
    }
