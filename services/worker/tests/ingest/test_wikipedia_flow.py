"""The Wikipedia jobs end to end: real Postgres, real Redis and a real RQ worker, with Wikipedia
replaced by the saved pages. No request leaves the machine."""

import io
import json
from datetime import UTC, date, datetime

import httpx
import pytest
from rq import SimpleWorker
from rq.job import Job
from rq.registry import FailedJobRegistry
from sqlalchemy import text

from cageops_scraper.config import ScraperSettings
from cageops_scraper.fetch import Fetcher
from cageops_scraper.sources import WikipediaSource
from cageops_worker.ingest import dlq, wikipedia_jobs
from cageops_worker.ingest.cli import main
from cageops_worker.ingest.config import IngestSettings
from cageops_worker.ingest.context import IngestContext, set_context
from cageops_worker.ingest.queue import get_queue
from cageops_worker.ingest.runs import start_backfill
from cageops_worker.ingest.upsert import read_counts

BASE = "https://en.wikipedia.org/wiki/"
USER_AGENT = "CageOps/0.1 (+https://github.com/ar1shah/cageops; test@cageops.dev)"
TODAY = date(2026, 10, 6)

TWO_EVENTS = """<html><body><div class="mw-heading"><h3 id="Past_events">Past events</h3></div>
<table class="wikitable"><tr><th>#</th><th>Event</th><th>Date</th></tr>
<tr><td>792</td><td><a href="https://en.wikipedia.org/wiki/UFC_332">UFC 332: Silva vs. Wang</a></td>
<td>Oct 3, 2026</td></tr>
<tr><td>778</td><td><a href="https://en.wikipedia.org/wiki/UFC_Fight_Night_279">UFC Fight Night:
Kape vs. Horiguchi</a></td><td>Jun 20, 2026</td></tr>{extra}</table></body></html>"""


class WikiSite:
    """en.wikipedia.org as the saved pages: what we have a fixture for is served, the rest 404s."""

    def __init__(self):
        self.pages: dict[str, tuple[int, str]] = {}
        self.requests: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        status, body = self.pages.get(url, (404, "<h1>Not Found</h1>"))
        return httpx.Response(status, text=body)


class FixedClock:
    def __call__(self) -> datetime:
        return datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


@pytest.fixture
def wiki_site(wiki_html):
    site = WikiSite()
    site.pages[BASE + "2026_in_UFC"] = (200, TWO_EVENTS.format(extra=""))
    site.pages[BASE + "UFC_332"] = (200, wiki_html("UFC_332"))
    site.pages[BASE + "UFC_Fight_Night_279"] = (200, wiki_html("UFC_Fight_Night_279"))
    return site


@pytest.fixture
def wctx(db, redis_client, wiki_site):
    settings = IngestSettings(_env_file=None, ingest_retry_base_s=0)
    fetcher = Fetcher(
        WikipediaSource(),
        db,
        redis_client,
        httpx.Client(transport=httpx.MockTransport(wiki_site)),
        ScraperSettings(scraper_user_agent=USER_AGENT, scraper_source="wikipedia", _env_file=None),
        sleep=lambda seconds: None,
        clock=FixedClock(),
    )
    context = IngestContext(
        engine=db,
        redis=redis_client,
        queue=get_queue(redis_client, settings),
        fetcher=fetcher,
        source=fetcher.source,
        settings=settings,
        today=lambda: TODAY,
    )
    set_context(context)
    yield context
    set_context(None)


@pytest.fixture
def work(wctx):
    def run() -> None:
        SimpleWorker([wctx.queue], connection=wctx.redis).work(burst=True, logging_level="CRITICAL")

    return run


def failed(wctx) -> dict[str, Job]:
    registry = FailedJobRegistry(queue=wctx.queue)
    return {i: Job.fetch(i, connection=wctx.redis) for i in registry.get_job_ids()}


def count(db, sql):
    with db.connect() as conn:
        return conn.execute(text(f"SELECT count(*) FROM {sql}")).scalar_one()


# -- discovery ---------------------------------------------------------------------------------


def test_the_real_year_list_queues_the_eighteen_events_the_seed_is_missing(
    wctx, wiki_site, wiki_html
):
    wiki_site.pages[BASE + "2026_in_UFC"] = (200, wiki_html("2026_in_UFC"))

    summary = wikipedia_jobs.wiki_discover_events("run", 2026, "2026-05-17")

    assert (summary["rows"], summary["events"], summary["without_article"]) == (34, 18, 0)
    ids = set(wctx.queue.job_ids)
    assert "wiki-event-UFC_332" in ids and "wiki-event-UFC_Fight_Night_277" in ids
    assert "wiki-event-UFC_Fight_Night_276" not in ids  # May 16: the seed already has it
    assert "next_year" not in summary


def test_a_since_date_in_an_earlier_year_queues_that_years_page_too(wctx):
    summary = wikipedia_jobs.wiki_discover_events("run", 2026, "2025-12-01")

    assert summary["next_year"] == 2025
    assert "wiki-discover-2025-since-2025-12-01" in set(wctx.queue.job_ids)


def test_an_event_with_no_article_is_counted_and_not_queued(wctx, wiki_site):
    row = "<tr><td>777</td><td>UFC Obscure Night</td><td>Jun 14, 2026</td></tr>"
    wiki_site.pages[BASE + "2026_in_UFC"] = (200, TWO_EVENTS.format(extra=row))

    summary = wikipedia_jobs.wiki_discover_events("run", 2026, "2026-06-01")

    assert (summary["events"], summary["without_article"]) == (2, 1)


# -- a whole backfill ----------------------------------------------------------------------------


def test_a_backfill_reads_the_list_then_each_event_and_writes_the_results(wctx, work, db):
    started = start_backfill(wctx, date(2026, 6, 1), run_id="r1")
    assert started.discovery.reason == "enqueued"

    work()

    assert failed(wctx) == {}
    assert (count(db, "events"), count(db, "fights")) == (2, 26)  # 12 + 14 bouts
    assert count(db, "fights WHERE result_source = 'wikipedia' AND ufcstats_id IS NULL") == 26
    rows = read_counts(wctx.redis, "r1")
    assert (rows["events:inserted"], rows["fights:inserted"]) == (2, 26)
    assert rows["fighters:inserted"] == count(db, "fighters")


def test_the_run_records_each_revision_and_each_fighter_it_created(wctx, work):
    start_backfill(wctx, date(2026, 6, 1), run_id="r1")
    work()
    out = io.StringIO()

    code = main(["status", "--run", "r1", "--json"], ctx=wctx, out=out)

    run = json.loads(out.getvalue())["run"]
    assert code == 0
    assert run["revisions"] == {
        "UFC_332": "1378760261",
        "UFC_Fight_Night:_Kape_vs._Horiguchi": "1369737950",  # the redirect's real title
    }
    assert "Natália_Silva_(fighter)" in run["stubs"] and len(run["stubs"]) > 20
    assert run["rows"]["fights"]["inserted"] == 26


def test_the_text_status_lists_revisions_and_stubs_for_review(wctx, work):
    start_backfill(wctx, date(2026, 6, 1), run_id="r1")
    work()
    out = io.StringIO()

    main(["status", "--run", "r1"], ctx=wctx, out=out)

    text_out = out.getvalue()
    assert "revisions read" in text_out and "UFC_332: 1378760261" in text_out
    assert "fighters created from Wikipedia" in text_out


def test_a_rerun_sends_no_request_and_writes_nothing(wctx, work, wiki_site, snapshot):
    start_backfill(wctx, date(2026, 6, 1), run_id="r1")
    work()
    before, requests = snapshot(), len(wiki_site.requests)

    start_backfill(wctx, date(2026, 6, 1), run_id="r2")  # every page is still fresh in raw_pages
    work()

    assert len(wiki_site.requests) == requests
    assert snapshot() == before
    assert failed(wctx) == {}
    rerun = read_counts(wctx.redis, "r2")
    assert rerun.get("fights:inserted", 0) == 0 and rerun.get("fights:updated", 0) == 0
    assert rerun["fights:unchanged"] == 26


def test_the_requests_are_only_ever_wiki_article_pages(wctx, work, wiki_site):
    start_backfill(wctx, date(2026, 6, 1), run_id="r1")
    work()

    assert sorted(wiki_site.requests) == [
        BASE + "2026_in_UFC",
        BASE + "UFC_332",
        BASE + "UFC_Fight_Night_279",
    ]  # no /w/, no /api/, no query string: robots.txt allows exactly these


# -- failures go to the dead-letter queue --------------------------------------------------------


def test_a_missing_article_and_a_broken_one_are_dead_lettered_and_the_rest_still_load(
    wctx, work, wiki_site, db
):
    rows = (
        "<tr><td>780</td><td><a href='https://en.wikipedia.org/wiki/UFC_Missing'>UFC Missing</a>"
        "</td><td>Jun 27, 2026</td></tr>"
        "<tr><td>779</td><td><a href='https://en.wikipedia.org/wiki/UFC_Broken'>UFC Broken</a>"
        "</td><td>Jul 11, 2026</td></tr>"
    )
    wiki_site.pages[BASE + "2026_in_UFC"] = (200, TWO_EVENTS.format(extra=rows))
    wiki_site.pages[BASE + "UFC_Broken"] = (
        200,
        "<html><body><h1>No results here</h1></body></html>",
    )

    start_backfill(wctx, date(2026, 6, 1), run_id="r1")
    work()

    letters = failed(wctx)
    assert set(letters) == {"wiki-event-UFC_Missing", "wiki-event-UFC_Broken"}
    assert letters["wiki-event-UFC_Missing"].meta["reason"] == "not_found"
    assert letters["wiki-event-UFC_Broken"].meta["reason"] == "parse_error"
    assert count(db, "events") == 2  # the two good articles were written


def test_a_suspect_duplicate_is_dead_lettered_with_the_link_then_replays_once_confirmed(
    wctx, work, db, wiki_page, monkeypatch
):
    link = next(
        b.second.link_title for b in wiki_page("UFC_332").bouts if b.second.name == "Wang Cong"
    )
    with db.begin() as conn:  # an existing fighter one letter off "Wang Cong"
        conn.execute(
            text(
                "INSERT INTO fighters (ufcstats_id, name) VALUES ('a000000000000001', 'Wang Congg')"
            )
        )
    start_backfill(wctx, date(2026, 6, 1), run_id="r1")
    work()

    letters = failed(wctx)
    assert set(letters) == {"wiki-event-UFC_332"}
    assert letters["wiki-event-UFC_332"].meta["reason"] == "mapping_error"
    assert f"(link '{link}')" in letters["wiki-event-UFC_332"].meta["error"]
    assert count(db, "events") == 1 and count(db, "fights") == 12  # only the other article

    monkeypatch.setattr(
        "cageops_worker.ingest.store_wikipedia.load_distinct_titles", lambda: frozenset({link})
    )  # a person confirmed it is a new fighter
    dlq.replay(wctx.queue, wctx.settings, "wiki-event-UFC_332")
    work()

    assert failed(wctx) == {}
    assert (count(db, "events"), count(db, "fights")) == (2, 26)


# -- the CLI ---------------------------------------------------------------------------------------


def test_the_cli_backfill_works_for_wikipedia(wctx):
    out = io.StringIO()

    code = main(["backfill", "--since", "2026-06-01"], ctx=wctx, out=out)

    assert code == 0
    assert "UFC event article" in out.getvalue()
    assert "wiki-discover-2026-since-2026-06-01" in set(wctx.queue.job_ids)


def test_scrape_upcoming_is_refused_for_wikipedia(wctx):
    out = io.StringIO()

    code = main(["scrape-upcoming"], ctx=wctx, out=out)

    assert code == 1 and "isn't supported for the wikipedia source" in out.getvalue()
    assert len(wctx.queue) == 0
