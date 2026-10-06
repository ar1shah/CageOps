"""Fixtures for the ingestion tests: the scraper's saved ufcstats pages, parsed on demand, a fake
ufcstats serving them, and a fully wired ingestion context on real Postgres and Redis."""

import re
import time
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
from rq import SimpleWorker
from rq.job import Job
from rq.registry import FailedJobRegistry
from sqlalchemy import text

from cageops_scraper.config import ScraperSettings
from cageops_scraper.fetch import Fetcher
from cageops_scraper.parsers.event import parse_event
from cageops_scraper.parsers.fight import parse_fight
from cageops_scraper.parsers.fighter import parse_fighter
from cageops_scraper.sources import UfcStatsSource
from cageops_worker.ingest.config import IngestSettings
from cageops_worker.ingest.context import IngestContext, set_context
from cageops_worker.ingest.queue import get_queue

FIXTURES = Path(__file__).parents[3] / "scraper" / "tests" / "fixtures" / "ufcstats"
BASE = "http://ufcstats.com"

BURNS_CARD = "c3ac8d0da7b05772"  # UFC Fight Night: Burns vs. Malott, 2026-04-18
UPCOMING_CARD = "7f98d9d5a10fa25c"  # UFC Fight Night: Allen vs. Duncan, 2026-10-10


@pytest.fixture
def html():
    """Raw text of a saved page: html("fight_32054bf2b36b0e47")."""
    return lambda name: (FIXTURES / f"{name}.html").read_text(encoding="utf-8")


@pytest.fixture
def fight(html):
    return lambda fid: parse_fight(html(f"fight_{fid}"), f"{BASE}/fight-details/{fid}")


@pytest.fixture
def fighter(html):
    return lambda fid: parse_fighter(html(f"fighter_{fid}"), f"{BASE}/fighter-details/{fid}")


@pytest.fixture
def burns_card(html):
    return parse_event(html(f"event_{BURNS_CARD}"), f"{BASE}/event-details/{BURNS_CARD}")


@pytest.fixture
def upcoming_card(html):
    return parse_event(
        html(f"event_upcoming_{UPCOMING_CARD}"), f"{BASE}/event-details/{UPCOMING_CARD}"
    )


@pytest.fixture
def bout_for(burns_card):
    """The Burns card's event-page row for a fight id (None for fights not on this card)."""
    by_id = {b.fight_id: b for b in burns_card.bouts}
    return by_id.get


# -- a fake ufcstats and a wired context for the job tests ----------------------------

USER_AGENT = "CageOps/0.1 (+https://github.com/ar1shah/cageops; test@cageops.dev)"
NOT_FOUND = (404, "<h1>Not Found</h1>")


class FakeSite:
    """ufcstats.com as the saved pages: every URL we have a fixture for is served, the rest 404.
    Records every request so a test can assert that a rerun sent none."""

    def __init__(self):
        self.pages: dict[str, tuple[int, str]] = {}
        self.requests: list[str] = []
        self.delay = 0.0  # seconds a response takes (to test job timeouts)
        self.flaky: dict[str, int] = {}  # path -> how many 503s to serve before the real page

    def add(self, path: str, body: str, status: int = 200) -> None:
        self.pages[f"{BASE}{path}"] = (status, body)

    def remove(self, path: str) -> None:
        self.pages.pop(f"{BASE}{path}", None)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if self.delay:
            time.sleep(self.delay)
        path = url.removeprefix(BASE)
        if self.flaky.get(path, 0) > 0:
            self.flaky[path] -= 1
            return httpx.Response(503, text="unavailable")
        status, body = self.pages.get(url, NOT_FOUND)
        return httpx.Response(status, text=body)


def _empty_list(html: str) -> str:
    """A list page with no events (what a page past the end of the history looks like)."""
    start, end = html.index("<tbody>"), html.index("</tbody>")
    return html[:start] + "<tbody></tbody>" + html[end + len("</tbody>") :]


@pytest.fixture
def site(html):
    fake = FakeSite()
    fake.add("/statistics/events/completed", html("events_completed"))
    fake.add("/statistics/events/completed?page=2", _empty_list(html("events_completed")))
    fake.add("/statistics/events/upcoming", html("events_upcoming"))
    fake.add(f"/event-details/{BURNS_CARD}", html(f"event_{BURNS_CARD}"))
    fake.add(f"/event-details/{UPCOMING_CARD}", html(f"event_upcoming_{UPCOMING_CARD}"))
    for name in sorted(p.stem for p in FIXTURES.glob("fight_*.html")):
        fake.add(f"/fight-details/{name.split('_')[1]}", html(name))
    for name in sorted(p.stem for p in FIXTURES.glob("fighter_*.html")):
        fake.add(f"/fighter-details/{name.split('_')[1]}", html(name))
    return fake


class FixedClock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def ctx(db, redis_client, site):
    """Real Postgres, real Redis, the fake site. Retries are instant (base 0) and nothing sleeps.
    Today is pinned to 2026-10-03, the day the fixtures were saved."""
    settings = IngestSettings(_env_file=None, ingest_retry_base_s=0)
    fetcher = Fetcher(
        UfcStatsSource(),
        db,
        redis_client,
        httpx.Client(transport=httpx.MockTransport(site)),
        ScraperSettings(scraper_user_agent=USER_AGENT, _env_file=None),
        sleep=lambda seconds: None,
        clock=FixedClock(datetime(2026, 10, 3, 12, 0, tzinfo=UTC)),
    )
    context = IngestContext(
        engine=db,
        redis=redis_client,
        queue=get_queue(redis_client, settings),
        fetcher=fetcher,
        source=fetcher.source,
        settings=settings,
        today=lambda: date(2026, 10, 3),
    )
    set_context(context)
    yield context
    set_context(None)


@pytest.fixture
def run_jobs(ctx):
    """Run a real RQ SimpleWorker until the queue is empty (burst mode)."""

    def run() -> None:
        SimpleWorker([ctx.queue], connection=ctx.redis).work(burst=True, logging_level="CRITICAL")

    return run


@pytest.fixture
def dead_letters(ctx):
    """{job_id: job} for everything in the failed registry."""

    def read() -> dict[str, Job]:
        registry = FailedJobRegistry(queue=ctx.queue)
        return {i: Job.fetch(i, connection=ctx.redis) for i in registry.get_job_ids()}

    return read


DIGEST = "SELECT md5(coalesce(string_agg(x::text, '|' ORDER BY x::text), '')) FROM {table} x"


@pytest.fixture
def snapshot(db):
    """A digest of every table the jobs write, to prove a rerun leaves them identical."""

    def take() -> dict[str, str]:
        tables = ("events", "fighters", "fights", "fight_totals", "fight_round_stats")
        with db.connect() as conn:
            return {t: conn.execute(text(DIGEST.format(table=t))).scalar_one() for t in tables}

    return take


def strip_row(html_text: str, fight_id: str) -> str:
    """The event page with one bout's row removed (that bout was taken off the card)."""
    pattern = re.compile(r'<tr[^>]*data-link="[^"]*' + fight_id + r'"[^>]*>.*?</tr>', re.S)
    stripped, n = pattern.subn("", html_text, count=1)
    assert n == 1, f"no row for {fight_id}"
    return stripped


# -- saved Wikipedia pages (CC BY-SA 4.0: see the NOTICE beside them) ---------------------------

WIKI_FIXTURES = Path(__file__).parents[3] / "scraper" / "tests" / "fixtures" / "wikipedia"


@pytest.fixture
def wiki_html():
    """A saved Wikipedia page by file name, e.g. wiki_html("UFC_323")."""

    def load(name: str) -> str:
        return (WIKI_FIXTURES / f"{name}.html").read_text(encoding="utf-8")

    return load


@pytest.fixture
def wiki_page(wiki_html):
    """A saved event article, parsed: wiki_page("UFC_323")."""
    from cageops_scraper.parsers.wikipedia_event import parse_event as parse_wiki_event

    return lambda name: parse_wiki_event(wiki_html(name), "https://en.wikipedia.org/wiki/" + name)
