from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from cageops_scraper.config import ScraperSettings
from cageops_scraper.sources import PageKind, UfcStatsSource, get_source

FIXTURES = Path(__file__).parent / "fixtures" / "ufcstats"
UA = "CageOps/0.1 (+https://github.com/ar1shah/cageops; test@cageops.dev)"
SOURCE = UfcStatsSource()


def test_detects_the_real_challenge_page_captured_on_2026_10_03():
    html = (FIXTURES / "challenge_2026-10-03.html").read_text(encoding="utf-8")

    assert SOURCE.block_reason(200, html) == "browser_challenge"


def test_403_is_a_block_and_a_normal_page_is_not():
    assert SOURCE.block_reason(403, "") == "http_403"
    assert SOURCE.block_reason(200, "<html><h2>Whittaker vs. Adesanya</h2></html>") is None
    assert SOURCE.block_reason(404, "<h1>Not Found</h1>") is None


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("http://ufcstats.com/fight-details/abc", "http://ufcstats.com/fight-details/abc"),
        ("https://ufcstats.com/fight-details/abc", "http://ufcstats.com/fight-details/abc"),
        ("http://www.UFCStats.com/fight-details/abc/", "http://ufcstats.com/fight-details/abc"),
        ("http://ufcstats.com/fight-details/abc#top", "http://ufcstats.com/fight-details/abc"),
        (
            "http://ufcstats.com/statistics/events/completed?page=all",
            "http://ufcstats.com/statistics/events/completed?page=all",
        ),
        (" http://ufcstats.com/robots.txt ", "http://ufcstats.com/robots.txt"),
    ],
)
def test_canonical_url_has_one_spelling_per_page(raw, canonical):
    assert SOURCE.canonical_url(raw) == canonical


def test_a_url_from_another_site_is_rejected():
    with pytest.raises(ValueError, match="not a ufcstats URL"):
        SOURCE.canonical_url("http://example.com/fight-details/abc")


D = date(2026, 4, 18)  # an event date


@pytest.mark.parametrize(
    ("kind", "status", "event_date", "ttl", "final_after"),
    [
        (PageKind.ROBOTS, 200, None, timedelta(hours=24), None),
        (PageKind.EVENTS_COMPLETED, 200, None, timedelta(hours=12), None),
        (PageKind.EVENTS_UPCOMING, 200, None, timedelta(hours=6), None),
        (PageKind.FIGHTER, 200, None, timedelta(days=30), None),
        (PageKind.FIGHT, 200, D, timedelta(hours=6), datetime(2026, 4, 21, tzinfo=UTC)),
        (PageKind.EVENT, 200, D, timedelta(hours=6), datetime(2026, 4, 21, tzinfo=UTC)),
        (PageKind.FIGHT, 200, None, timedelta(hours=6), None),  # unknown date: assume it changes
        (PageKind.FIGHT, 404, D, timedelta(days=1), None),
        (PageKind.FIGHTER, 404, None, timedelta(days=1), None),
    ],
)
def test_cache_policy_table(kind, status, event_date, ttl, final_after):
    policy = SOURCE.cache_policy(kind, status, event_date)

    assert (policy.ttl, policy.final_after) == (ttl, final_after)


def test_fight_page_is_fresh_for_six_hours_then_final_once_fetched_after_settling():
    policy = SOURCE.cache_policy(PageKind.FIGHT, 200, D)
    night = datetime(2026, 4, 19, 6, tzinfo=UTC)  # the morning after the event
    settled = datetime(2026, 4, 21, 12, tzinfo=UTC)  # 3+ days after

    assert policy.is_fresh(night, night + timedelta(hours=5))
    assert not policy.is_fresh(night, night + timedelta(hours=7))
    assert policy.is_fresh(settled, settled + timedelta(days=700))  # final


def test_real_source_must_use_the_real_host_and_replay_must_not():
    with pytest.raises(ValueError, match="cannot use base URL"):
        UfcStatsSource("ufcstats", "http://127.0.0.1:8099")
    with pytest.raises(ValueError, match="cannot use base URL"):
        UfcStatsSource("ufcstats_replay", "http://ufcstats.com")
    replay = UfcStatsSource("ufcstats_replay", "http://127.0.0.1:8099")
    assert replay.canonical_url("http://127.0.0.1:8099/fight-details/abc/") == (
        "http://127.0.0.1:8099/fight-details/abc"
    )
    with pytest.raises(ValueError):
        replay.canonical_url("http://ufcstats.com/fight-details/abc")  # wrong site for replay


def test_get_source_picks_by_name_and_replay_is_a_separate_source():
    real = get_source(ScraperSettings(scraper_user_agent=UA, _env_file=None))
    replay = get_source(
        ScraperSettings(scraper_user_agent=UA, scraper_source="ufcstats_replay", _env_file=None)
    )

    assert (real.name, replay.name) == ("ufcstats", "ufcstats_replay")
    assert replay.base_url == "http://127.0.0.1:8099"
