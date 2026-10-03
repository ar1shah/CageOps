"""Fixtures for fetcher tests: a real Postgres and Redis, and a fake website (httpx.MockTransport).

No test here makes a network request: the "site" is a Python function that records every
request it receives, so a test can assert that a request was (or was not) sent.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from cageops_scraper.config import ScraperSettings
from cageops_scraper.fetch import Fetcher
from cageops_scraper.sources import UfcStatsSource

UA = "CageOps/0.1 (+https://github.com/ar1shah/cageops; test@cageops.dev)"
FIXTURES = Path(__file__).parent / "fixtures" / "ufcstats"


class Clock:
    """A clock the test moves by hand."""

    def __init__(self):
        self.now = datetime(2026, 4, 19, 6, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


class FakeSite:
    """Answers requests from a dict of {url: (status, body, headers)} and records them all."""

    def __init__(self):
        self.pages: dict[str, tuple[int, str, dict[str, str]]] = {}
        self.requests: list[httpx.Request] = []
        self.raise_on: dict[str, Exception] = {}

    def add(self, url: str, body: str = "<html>ok</html>", status: int = 200, **headers):
        self.pages[url] = (status, body, headers)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url in self.raise_on:
            raise self.raise_on[url]
        status, body, headers = self.pages.get(url, (404, "<h1>Not Found</h1>", {}))
        return httpx.Response(status, text=body, headers=headers)

    @property
    def urls(self) -> list[str]:
        return [str(r.url) for r in self.requests]


@pytest.fixture
def fixture_html():
    """Load a saved ufcstats page by name, e.g. fixture_html("fight_32054bf2b36b0e47")."""

    def load(name: str) -> str:
        return (FIXTURES / f"{name}.html").read_text(encoding="utf-8")

    return load


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def site() -> FakeSite:
    return FakeSite()


@pytest.fixture
def sleeps() -> list[float]:
    """Every sleep the fetcher asked for. Tests never really wait."""
    return []


@pytest.fixture
def challenge_html() -> str:
    return (FIXTURES / "challenge_2026-10-03.html").read_text(encoding="utf-8")


@pytest.fixture
def make_fetcher(db, redis_client, site, clock, sleeps):
    """Build a Fetcher on the real test Postgres and Redis, talking to the fake site."""

    def build(**overrides) -> Fetcher:
        settings = ScraperSettings(scraper_user_agent=UA, _env_file=None)
        return Fetcher(
            source=overrides.pop("source", UfcStatsSource()),
            engine=db,
            redis=redis_client,
            client=httpx.Client(transport=httpx.MockTransport(site)),
            settings=settings,
            sleep=overrides.pop("sleep", sleeps.append),
            clock=clock,
        )

    return build


@pytest.fixture
def fetcher(make_fetcher) -> Fetcher:
    return make_fetcher()
