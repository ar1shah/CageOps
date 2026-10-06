from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from cageops_scraper.breaker import CircuitBreaker
from cageops_scraper.cache import PageCache
from cageops_scraper.config import ScraperSettings
from cageops_scraper.ratelimit import RateLimiter
from cageops_scraper.sources import UfcStatsSource

UA = "CageOps/0.1 (+https://github.com/ar1shah/cageops; test@cageops.dev)"


def make(**kwargs) -> ScraperSettings:
    return ScraperSettings(_env_file=None, **kwargs)


def test_user_agent_is_required(monkeypatch):
    monkeypatch.delenv("SCRAPER_USER_AGENT", raising=False)

    with pytest.raises(ValidationError, match="scraper_user_agent"):
        make()


def test_the_placeholder_contact_from_env_example_is_rejected():
    with pytest.raises(ValidationError, match="placeholder"):
        make(
            scraper_user_agent="CageOps/0.1 (+https://github.com/ar1shah/cageops; you@example.com)"
        )


def test_real_site_cannot_be_scraped_faster_than_one_request_per_second():
    with pytest.raises(ValidationError, match="SCRAPER_MIN_INTERVAL_MS"):
        make(scraper_user_agent=UA, scraper_min_interval_ms=500)


def test_replay_server_may_go_faster():
    settings = make(
        scraper_user_agent=UA, scraper_source="ufcstats_replay", scraper_min_interval_ms=50
    )

    assert settings.scraper_min_interval_ms == 50


@pytest.mark.parametrize("interval", [0, 100, 500, 999])
def test_relaxed_interval_is_rejected_for_the_real_source(interval):
    """The benchmark's 100 ms setting must be impossible against the real site."""
    with pytest.raises(ValidationError, match="SCRAPER_MIN_INTERVAL_MS"):
        make(scraper_user_agent=UA, scraper_source="ufcstats", scraper_min_interval_ms=interval)


def test_the_real_sites_floor_is_exactly_1000_ms():
    settings = make(scraper_user_agent=UA, scraper_source="ufcstats", scraper_min_interval_ms=1000)

    assert settings.scraper_min_interval_ms == 1000


@pytest.mark.parametrize(
    "url",
    [
        "http://ufcstats.com",
        "http://www.ufcstats.com",
        "https://ufcstats.com/statistics/events/completed",
        "http://203.0.113.9:8099",
        "http://example.org",
        "http://127.0.0.1.ufcstats.com",  # looks like loopback, isn't
    ],
)
def test_replay_source_must_point_at_loopback(url):
    """Otherwise SCRAPER_SOURCE=ufcstats_replay would skip the floor against the real site."""
    with pytest.raises(ValidationError, match="UFCSTATS_REPLAY_BASE_URL"):
        make(
            scraper_user_agent=UA,
            scraper_source="ufcstats_replay",
            scraper_min_interval_ms=50,
            ufcstats_replay_base_url=url,
        )


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:8099", "http://localhost:8099", "http://[::1]:8099"]
)
def test_replay_source_accepts_loopback(url):
    settings = make(
        scraper_user_agent=UA,
        scraper_source="ufcstats_replay",
        scraper_min_interval_ms=50,
        ufcstats_replay_base_url=url,
    )

    assert settings.scraper_min_interval_ms == 50


def test_the_replay_source_never_shares_state_with_the_real_one(redis_client, db):
    """D-015: separate cached pages, rate limit and circuit breaker, so a replay run can't use up
    the real site's request budget or trip (or hide) its breaker."""
    real = UfcStatsSource("ufcstats", "http://ufcstats.com")
    replay = UfcStatsSource("ufcstats_replay", "http://127.0.0.1:8099")

    assert real.name != replay.name
    assert RateLimiter(redis_client, real.name).key != RateLimiter(redis_client, replay.name).key
    assert (
        CircuitBreaker(redis_client, real.name).key != CircuitBreaker(redis_client, replay.name).key
    )
    CircuitBreaker(redis_client, replay.name).trip(
        "x", "http://127.0.0.1:8099/x", datetime.now(UTC)
    )
    assert CircuitBreaker(redis_client, real.name).state() is None  # the real one is untouched
    now = datetime.now(UTC)
    PageCache(db, real.name).put("http://ufcstats.com/x", 200, "<p>real</p>", now)
    assert PageCache(db, replay.name).get("http://ufcstats.com/x") is None  # never returned


def test_env_variables_are_read(monkeypatch):
    monkeypatch.setenv("SCRAPER_USER_AGENT", UA)
    monkeypatch.setenv("SCRAPER_TIMEOUT_S", "7")

    settings = make()

    assert settings.scraper_user_agent == UA
    assert settings.scraper_timeout_s == 7


def test_a_quoted_value_in_a_dotenv_file_is_read_without_the_quotes(tmp_path):
    env = tmp_path / ".env"
    env.write_text(f'SCRAPER_USER_AGENT="{UA}"\n')

    settings = ScraperSettings(_env_file=env)

    assert settings.scraper_user_agent == UA


def test_env_example_has_a_quoted_user_agent_and_still_fails_on_the_placeholder():
    from pathlib import Path

    example = Path(__file__).parents[3] / ".env.example"

    with pytest.raises(ValidationError, match="placeholder"):
        ScraperSettings(_env_file=example)
    line = next(x for x in example.read_text().splitlines() if x.startswith("SCRAPER_USER_AGENT="))
    assert line.split("=", 1)[1].startswith('"') and line.endswith('"')
