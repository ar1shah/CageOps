"""The synthetic corpus run through the real pipeline: this is what the benchmark measures, at a
size small enough for a test."""

import logging
from datetime import date

import pytest
from replay_corpus import Corpus
from sqlalchemy import text

from cageops_worker.ingest.runs import enqueue_backfill
from cageops_worker.ingest.upsert import read_counts

EVENTS = 3
SINCE = date(2026, 1, 1)


@pytest.fixture
def corpus():
    return Corpus(events=EVENTS)


@pytest.fixture
def replay(site, corpus):
    """The fake site serving the synthetic corpus instead of the 14 fixture pages."""
    site.pages.clear()
    for path in corpus.paths():
        site.add(path, corpus.render(path))
    return site


def count(db, table: str) -> int:
    with db.connect() as conn:
        return conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()


def test_a_backfill_of_the_corpus_stores_exactly_what_the_corpus_holds(
    ctx, replay, run_jobs, dead_letters, db, corpus, caplog
):
    caplog.set_level(logging.WARNING)
    run_id = enqueue_backfill(ctx, SINCE)

    run_jobs()

    # the one catch-weight bout on the template card is a known, documented anomaly (D-023)
    kinds = {getattr(r, "kind", None) for r in caplog.records if "anomaly" in r.getMessage()}
    assert kinds <= {"gender_assumed"}

    assert dead_letters() == {}
    assert count(db, "events") == EVENTS
    assert count(db, "fights") == EVENTS * 4
    assert count(db, "fighters") == EVENTS * 8
    assert count(db, "fight_totals") > 0 and count(db, "fight_round_stats") > 0
    assert len(replay.requests) == corpus.expected_requests()  # each page exactly once
    counts = read_counts(ctx.redis, run_id)
    assert counts["events:inserted"] == EVENTS and counts["fights:inserted"] == EVENTS * 4
    # a fighter row is inserted (name only) by the fight job, then updated by the fighter job
    assert counts["fighters:inserted"] == EVENTS * 8 and counts["fighters:updated"] == EVENTS * 8
    assert not any(key.endswith(":updated") for key in counts if not key.startswith("fighters"))


def test_the_fights_take_their_dates_from_their_synthetic_event(ctx, replay, run_jobs, db, corpus):
    enqueue_backfill(ctx, SINCE)
    run_jobs()

    with db.connect() as conn:
        dates = set(conn.execute(text("SELECT event_date FROM events")).scalars())

    assert dates == {corpus.event_date(k) for k in range(EVENTS)}


def test_a_rerun_sends_no_requests_and_writes_nothing(ctx, replay, run_jobs, snapshot):
    enqueue_backfill(ctx, SINCE)
    run_jobs()
    before, requests = snapshot(), len(replay.requests)

    run_id = enqueue_backfill(ctx, SINCE)
    run_jobs()

    assert len(replay.requests) == requests
    assert snapshot() == before
    counts = read_counts(ctx.redis, run_id)
    assert not any(key.endswith((":inserted", ":updated")) for key in counts)


def test_a_warm_run_reinserts_everything_from_the_cache_with_no_requests(
    ctx, replay, run_jobs, db, redis_client, dead_letters
):
    """Warm = pages cached, tables empty: the same rows come back with zero network requests."""
    enqueue_backfill(ctx, SINCE)
    run_jobs()
    requests = len(replay.requests)
    with db.begin() as conn:
        conn.execute(
            text("TRUNCATE fight_round_stats, fight_totals, fights, fighters, events CASCADE")
        )
    redis_client.flushdb()  # a fresh queue, claims and counters, like a new benchmark run

    run_id = enqueue_backfill(ctx, SINCE)
    run_jobs()

    assert len(replay.requests) == requests  # nothing fetched
    assert dead_letters() == {}
    assert count(db, "fights") == EVENTS * 4
    assert read_counts(ctx.redis, run_id)["fights:inserted"] == EVENTS * 4
