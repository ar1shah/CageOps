import re
from datetime import UTC, date, datetime

import pytest
from rq.job import Job
from sqlalchemy import text

from cageops_scraper.errors import NotFound, SourceBlocked
from cageops_scraper.parsers.events import parse_events_list
from cageops_worker.ingest import jobs, store
from cageops_worker.ingest.errors import MissingPrerequisite
from cageops_worker.ingest.mapping import map_scheduled_bout
from cageops_worker.ingest.queue import claim_state
from cageops_worker.ingest.runs import enqueue_backfill, enqueue_upcoming
from cageops_worker.ingest.upsert import read_counts

from .conftest import BASE, BURNS_CARD, UPCOMING_CARD, strip_row

BURNS_URL = f"{BASE}/event-details/{BURNS_CARD}"
UPCOMING_URL = f"{BASE}/event-details/{UPCOMING_CARD}"
BURNS_FIGHTS_WITH_PAGES = {
    "32054bf2b36b0e47",
    "b5299e5b946015e5",
    "9fab4b0ad082f670",
    "552f7cdaf93e1055",
}


def scalar(db, sql, **params):
    with db.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def queued(ctx):
    return set(ctx.queue.job_ids)


# -- a whole backfill, end to end ------------------------------------------------------


@pytest.fixture
def backfilled(ctx, run_jobs):
    """Run a backfill of the one event we have pages for (the Burns card, 2026-04-18)."""
    ctx.today = lambda: date(2026, 4, 19)
    run_id = enqueue_backfill(ctx, date(2026, 4, 18))
    run_jobs()
    return run_id


def test_a_backfill_walks_list_event_fights_and_fighters_into_the_tables(
    ctx, backfilled, db, site, dead_letters
):
    assert scalar(db, "SELECT count(*) FROM events") == 1
    assert scalar(db, "SELECT event_date FROM events") == date(2026, 4, 18)
    played = set(
        scalar(db, "SELECT array_agg(ufcstats_id) FROM completed_fights")  # only finished fights
    )
    assert played == BURNS_FIGHTS_WITH_PAGES
    assert scalar(db, "SELECT count(*) FROM fight_totals") == 8
    assert scalar(db, "SELECT count(*) FROM fight_round_stats") == 20  # 6 + 6 + 2 + 6
    assert scalar(db, "SELECT height_cm FROM fighters WHERE name = 'Gilbert Burns'") == 177.8
    # list + event + 12 fights + 24 fighters
    assert len(site.requests) == 1 + 1 + 12 + 24
    assert len(ctx.queue) == 0


def test_run_totals_say_what_was_inserted(ctx, backfilled):
    counts = read_counts(ctx.redis, backfilled)

    assert counts["events:inserted"] == 1
    assert counts["fights:inserted"] == 4
    assert counts["fight_totals:inserted"] == 8
    assert counts["fight_round_stats:inserted"] == 20
    assert counts["fighters:updated"] == 1  # Burns: the stub from his fight gets its bio


def test_pages_the_site_doesnt_have_end_up_in_the_dead_letter_queue_with_a_reason(
    ctx, backfilled, dead_letters
):
    letters = dead_letters()

    # 8 bouts and 23 fighters have no saved page, so the fake site 404s them
    assert len(letters) == 8 + 23
    assert {j.meta["reason"] for j in letters.values()} == {"not_found"}
    assert {j.meta["attempts"] for j in letters.values()} == {1}  # a 404 is never retried
    one = letters["fighter-" + next(i for i in ("5fa2974cbd18e05c",))]
    assert one.meta["error_class"] == "NotFound" and one.meta["job_type"] == "fetch_fighter"
    assert one.meta["url"].endswith("/fighter-details/5fa2974cbd18e05c")


def test_claims_are_released_on_success_and_kept_for_dead_letters(ctx, backfilled):
    assert claim_state(ctx.redis, f"event-{BURNS_CARD}") is None
    assert claim_state(ctx.redis, "fight-32054bf2b36b0e47") is None
    assert claim_state(ctx.redis, "fight-" + "0" * 15 + "1") is None  # never queued
    # a dead-lettered job keeps a permanent claim so a rerun doesn't pile onto it
    letter_claims = [
        claim_state(ctx.redis, f"fighter-{i}") for i in ("5fa2974cbd18e05c", "2f181c0467965b98")
    ]
    assert "dead_lettered" in letter_claims


def test_running_the_same_backfill_again_sends_no_requests_and_changes_nothing(
    ctx, backfilled, run_jobs, site, db, snapshot
):
    before, requests = snapshot(), len(site.requests)

    second = enqueue_backfill(ctx, date(2026, 4, 18))
    run_jobs()

    assert len(site.requests) == requests  # every page came from the cache
    assert snapshot() == before  # not one row changed
    counts = read_counts(ctx.redis, second)
    assert {k for k in counts if not k.endswith(":unchanged")} == set()
    assert counts["fights:unchanged"] == 4 and counts["fight_round_stats:unchanged"] == 20
    assert counts["events:unchanged"] == 1


def test_the_fight_dates_come_from_the_event_row_not_the_fight_pages(ctx, backfilled, db):
    dates = scalar(
        db,
        "SELECT array_agg(DISTINCT e.event_date) FROM completed_fights f"
        " JOIN events e ON e.id = f.event_id",
    )

    assert dates == [date(2026, 4, 18)]
    columns = scalar(
        db,
        "SELECT count(*) FROM information_schema.columns WHERE table_name = 'fights'"
        " AND (column_name LIKE '%date%')",
    )
    assert columns == 0  # the fights table has no date column at all


# -- discovery -------------------------------------------------------------------------


def completed_rows(html):
    return {r.event_date: r for r in parse_events_list(html("events_completed"), BASE)}


def test_discovery_pages_back_while_the_page_still_reaches_since(ctx, html):
    ctx.today = lambda: date(2026, 4, 19)
    rows = completed_rows(html)

    summary = jobs.discover_events("completed", "run", 1, "2026-04-11")

    assert summary["next_page"] == 2  # this page's oldest row (2026-04-11) is not before `since`
    assert queued(ctx) == {
        f"event-{rows[date(2026, 4, 18)].ufcstats_id}",
        f"event-{rows[date(2026, 4, 11)].ufcstats_id}",
        "discover-completed-p2-since-2026-04-11",
    }


def test_discovery_stops_when_the_page_already_reaches_before_since(ctx, html):
    ctx.today = lambda: date(2026, 4, 19)

    summary = jobs.discover_events("completed", "run", 1, "2026-04-18")

    assert "next_page" not in summary
    assert len(ctx.queue) == 1  # just the one event dated 2026-04-18


def test_a_page_past_the_end_of_the_history_has_no_rows_and_stops(ctx):
    summary = jobs.discover_events("completed", "run", 2, "2026-04-11")

    assert (summary["rows"], len(ctx.queue)) == (0, 0)


def test_the_next_upcoming_event_on_the_completed_list_is_never_treated_as_completed(ctx):
    jobs.discover_events("completed", "run", 1, "2026-09-01")  # today is 2026-10-03

    ids = queued(ctx)
    assert f"event-{UPCOMING_CARD}" not in ids  # dated 2026-10-10, flagged "next" on the list
    assert "event-ad3fdba28a7540cf" in ids  # UFC 332, 2026-10-03


def test_the_upcoming_list_queues_every_listed_event(ctx):
    summary = jobs.discover_events("upcoming", "run")

    assert summary["events"] == 8 and len(ctx.queue) == 8
    assert f"event-{UPCOMING_CARD}" in queued(ctx)


def test_an_event_that_dropped_off_the_upcoming_list_is_rechecked_not_assumed_cancelled(
    ctx, site, html
):
    jobs.fetch_event(UPCOMING_URL, "2026-10-10", "run")  # we now hold 12 scheduled bouts
    ctx.queue.empty()
    listing = html("events_upcoming")
    dropped = re.sub(
        r'<tr class="b-statistics__table-row">\s*<td(?:(?!</tr>).)*' + UPCOMING_CARD + r".*?</tr>",
        "",
        listing,
        count=1,
        flags=re.S,
    )
    assert dropped != listing
    site.add("/statistics/events/upcoming", dropped)

    summary = jobs.discover_events("upcoming", "run2", force=True)

    assert summary["rechecked"] == 1
    job = Job.fetch(f"event-{UPCOMING_CARD}", connection=ctx.redis)
    assert job.kwargs["force"] is True  # the cached page can't tell us the card was dropped


def test_scheduled_bouts_long_past_their_event_are_flagged_as_stale(ctx, db, upcoming_card, caplog):
    stale_event = {"ufcstats_id": "a" * 16, "name": "Old card", "event_date": date(2026, 9, 20),
                   "city": None, "state": None, "country": None}  # fmt: skip
    with db.begin() as conn:
        store.write_event(conn, stale_event)
        store.write_scheduled_bouts(
            conn, "a" * 16, [map_scheduled_bout(upcoming_card.bouts[0], "a" * 16)]
        )

    with caplog.at_level("WARNING"):
        jobs.discover_events("upcoming", "run")

    details = [getattr(r, "detail", "") for r in caplog.records]
    assert "stale_scheduled_bout:count=1" in details  # 13 days after the event, no result


# -- the event job ---------------------------------------------------------------------


def test_an_upcoming_card_gives_scheduled_bouts_and_fighter_jobs_but_no_fight_jobs(ctx, db):
    summary = jobs.fetch_event(UPCOMING_URL, "2026-10-10", "run")

    assert (summary["scheduled"], summary["fights_enqueued"], summary["fighters"]) == (12, 0, 24)
    assert scalar(db, "SELECT count(*) FROM fights WHERE status = 'scheduled'") == 12
    assert len(ctx.queue) == 24 and all(i.startswith("fighter-") for i in queued(ctx))


def test_running_the_event_job_twice_changes_nothing_and_queues_nothing_new(ctx, db, snapshot):
    jobs.fetch_event(UPCOMING_URL, "2026-10-10", "run")
    before = snapshot()

    jobs.fetch_event(UPCOMING_URL, "2026-10-10", "run-2")

    assert snapshot() == before
    assert len(ctx.queue) == 24  # the claims turned all 24 repeat enqueues into no-ops
    assert read_counts(ctx.redis, "run-2")["fights_scheduled:unchanged"] == 12


def test_a_bout_taken_off_the_card_is_cancelled_on_the_next_fetch(
    ctx, db, site, html, upcoming_card
):
    jobs.fetch_event(UPCOMING_URL, "2026-10-10", "run")
    removed = upcoming_card.bouts[-1].fight_id
    site.add(
        f"/event-details/{UPCOMING_CARD}",
        strip_row(html(f"event_upcoming_{UPCOMING_CARD}"), removed),
    )

    summary = jobs.fetch_event(UPCOMING_URL, "2026-10-10", "run-2", force=True)

    assert summary["cancelled"] == 1
    assert scalar(db, "SELECT status FROM fights WHERE ufcstats_id = :u", u=removed) == "cancelled"
    assert scalar(db, "SELECT count(*) FROM fights WHERE status = 'scheduled'") == 11


def test_an_event_page_that_disappears_cancels_its_scheduled_bouts(ctx, db, site):
    jobs.fetch_event(UPCOMING_URL, "2026-10-10", "run")
    site.add(f"/event-details/{UPCOMING_CARD}", "<h1>Not Found</h1>", status=404)

    summary = jobs.fetch_event(UPCOMING_URL, "2026-10-10", "run-2", force=True)

    assert summary["gone"] is True and summary["cancelled"] == 12
    assert scalar(db, "SELECT count(*) FROM fights WHERE status = 'cancelled'") == 12


def test_a_404_for_an_event_we_never_held_is_just_a_failure(ctx, site):
    site.remove(f"/event-details/{BURNS_CARD}")

    with pytest.raises(NotFound):
        jobs.fetch_event(BURNS_URL, "2026-04-18", "run")


def test_the_event_pages_date_wins_over_the_lists_and_the_disagreement_is_reported(ctx, db, caplog):
    with caplog.at_level("WARNING"):
        jobs.fetch_event(BURNS_URL, "2026-04-17", "run")  # the list said 04-17, the page says 04-18

    assert scalar(db, "SELECT event_date FROM events") == date(2026, 4, 18)
    assert "event_date_mismatch:list=2026-04-17:page=2026-04-18" in [
        getattr(r, "detail", "") for r in caplog.records
    ]


# -- the fight and fighter jobs --------------------------------------------------------


def test_a_fight_job_refuses_to_run_without_its_event_row(ctx, burns_card):
    bout = next(b for b in burns_card.bouts if b.fight_id == "32054bf2b36b0e47")

    with pytest.raises(MissingPrerequisite, match="takes its date from the event page"):
        jobs.fetch_fight(
            f"{BASE}/fight-details/32054bf2b36b0e47",
            BURNS_CARD,
            "2026-04-18",
            bout.model_dump(mode="json"),
            "run",
        )


def test_a_fight_job_reports_what_it_wrote(ctx, burns_card):
    jobs.fetch_event(BURNS_URL, "2026-04-18", "run")
    bout = next(b for b in burns_card.bouts if b.fight_id == "32054bf2b36b0e47")

    result = jobs.fetch_fight(
        f"{BASE}/fight-details/32054bf2b36b0e47", BURNS_CARD, "2026-04-18",
        bout.model_dump(mode="json"), "run",
    )  # fmt: skip

    assert result["fights"] == {"inserted": 1, "updated": 0, "unchanged": 0}
    assert result["fight_round_stats"]["inserted"] == 6 and result["was"] is None


def test_a_fighter_job_fills_in_the_bio(ctx, db):
    result = jobs.fetch_fighter(f"{BASE}/fighter-details/23024fdfc966410a", "run")

    assert result["fighters"]["inserted"] == 1
    assert scalar(db, "SELECT reach_cm FROM fighters") == 180.34


# -- starting a run --------------------------------------------------------------------


def test_no_work_is_queued_for_a_site_that_has_blocked_us(ctx):
    ctx.fetcher.breaker.trip("browser_challenge", f"{BASE}/x", datetime(2026, 10, 3, tzinfo=UTC))

    with pytest.raises(SourceBlocked):
        enqueue_backfill(ctx, date(2026, 4, 18))
    with pytest.raises(SourceBlocked):
        enqueue_upcoming(ctx)
    assert len(ctx.queue) == 0


def test_starting_the_same_run_twice_queues_one_discovery_job(ctx):
    enqueue_backfill(ctx, date(2026, 4, 18))
    enqueue_backfill(ctx, date(2026, 4, 18))

    assert ctx.queue.job_ids == ["discover-completed-p1-since-2026-04-18"]


def test_the_discovery_job_for_a_different_since_is_a_different_job(ctx):
    enqueue_backfill(ctx, date(2026, 4, 18))
    enqueue_backfill(ctx, date(2025, 10, 3))

    assert len(ctx.queue) == 2
