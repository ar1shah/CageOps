"""WikipediaSource: only /wiki/ article pages can be asked for, and how long a copy stays good."""

from datetime import UTC, date, datetime, timedelta

import pytest

from cageops_scraper.config import ScraperSettings
from cageops_scraper.sources import PageKind, UfcStatsSource, WikipediaSource, get_source

UA = "CageOps/0.1 (+https://github.com/ar1shah/cageops; test@cageops.dev)"
BASE = "https://en.wikipedia.org"
source = WikipediaSource()
NIGHT = "UFC_Fight_Night_279:_Kape_vs._Horiguchi"


# -- canonical_url: one spelling per article, and nothing robots.txt forbids -----------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (f"{BASE}/wiki/UFC_332", f"{BASE}/wiki/UFC_332"),
        (f"{BASE}/wiki/UFC 332", f"{BASE}/wiki/UFC_332"),  # space = underscore
        (f"{BASE}/wiki/UFC%20332", f"{BASE}/wiki/UFC_332"),
        (f"{BASE}/wiki/UFC_332#Results", f"{BASE}/wiki/UFC_332"),  # a fragment is never sent
        ("http://en.wikipedia.org/wiki/UFC_332", f"{BASE}/wiki/UFC_332"),  # scheme from the base
        (f"{BASE}/wiki/{NIGHT}", f"{BASE}/wiki/{NIGHT}"),
        (f"{BASE}/wiki/UFC_Fight_Night_279%3A_Kape_vs._Horiguchi", f"{BASE}/wiki/{NIGHT}"),
        (f"{BASE}/wiki/Natália_Silva", f"{BASE}/wiki/Nat%C3%A1lia_Silva"),  # non-ASCII escaped
        (f"{BASE}/wiki/Nat%C3%A1lia_Silva", f"{BASE}/wiki/Nat%C3%A1lia_Silva"),
        (f"{BASE}/wiki/Jon_Jones_(fighter)", f"{BASE}/wiki/Jon_Jones_(fighter)"),
        (f"{BASE}/robots.txt", f"{BASE}/robots.txt"),
        ("https://EN.wikipedia.org/wiki/2026_in_UFC", f"{BASE}/wiki/2026_in_UFC"),
    ],
)  # fmt: skip
def test_canonical_url_gives_one_spelling_per_article(given, expected):
    assert source.canonical_url(given) == expected


@pytest.mark.parametrize(
    "forbidden",
    [
        f"{BASE}/w/api.php?action=parse&page=UFC_332&format=json",  # Action API: /w/ disallowed
        f"{BASE}/w/index.php?title=UFC_332&action=raw",
        f"{BASE}/w/index.php?title=UFC_332&oldid=1336221891",
        f"{BASE}/api/rest_v1/page/html/UFC_332",  # the REST API: /api/ is disallowed
        f"{BASE}/wiki/UFC_332?action=raw",  # any query string
        f"{BASE}/wiki/Special:Search?search=ufc",
        f"{BASE}/wiki/special:random",  # case-insensitive
        f"{BASE}/wiki/Special%3ARandom",  # percent-encoded colon
        f"{BASE}/wiki/Talk:UFC_332",
        f"{BASE}/wiki/User:Someone",
        f"{BASE}/wiki/Wikipedia:Articles_for_deletion/UFC_332",
        f"{BASE}/wiki/Category:UFC_events",
        f"{BASE}/wiki/File:Octagon.jpg",
        f"{BASE}/wiki/",
        f"{BASE}/",
        f"{BASE}/robots.txt?x=1",
        "https://en.m.wikipedia.org/wiki/UFC_332",  # other hosts
        "https://de.wikipedia.org/wiki/UFC_332",
        "http://ufcstats.com/fight-details/aaaa000000000001",
    ],
)
def test_canonical_url_makes_forbidden_urls_impossible(forbidden):
    with pytest.raises(ValueError):
        source.canonical_url(forbidden)


def test_the_source_only_talks_to_the_english_wikipedia():
    with pytest.raises(ValueError, match="only talks to en.wikipedia.org"):
        WikipediaSource("https://example.org")


# -- block_reason, slow_response -----------------------------------------------------------------


def test_a_403_means_we_are_blocked_but_a_429_is_just_a_slow_down():
    assert source.block_reason(403, "") == "http_403"
    assert source.block_reason(429, "") is None
    assert source.block_reason(200, "<html>Checking your browser</html>") is None


def test_wikipedia_states_a_slow_response_rule_and_ufcstats_does_not():
    assert (source.slow_response.threshold_s, source.slow_response.pause_s) == (1.0, 5.0)
    assert UfcStatsSource().slow_response is None


# -- cache policy: 24 h for 30 days after the event, then final ----------------------------------

EVENT = date(2026, 10, 3)
EVENT_FINAL = datetime(2026, 11, 2, tzinfo=UTC)  # event date + 30 days


def fresh(policy, fetched_at, now):
    return policy.is_fresh(fetched_at, now)


def test_an_event_article_is_refetched_daily_until_30_days_after_the_event():
    policy = source.cache_policy(PageKind.EVENT, 200, EVENT)
    fetched = datetime(2026, 10, 10, 12, tzinfo=UTC)

    assert fresh(policy, fetched, fetched + timedelta(hours=23))
    assert not fresh(policy, fetched, fetched + timedelta(hours=25))  # a vandalised copy heals


def test_an_event_article_fetched_29_days_after_still_goes_stale():
    policy = source.cache_policy(PageKind.EVENT, 200, EVENT)
    fetched = EVENT_FINAL - timedelta(hours=12)

    assert not fresh(policy, fetched, fetched + timedelta(days=2))


def test_an_event_article_fetched_30_days_after_the_event_is_final():
    policy = source.cache_policy(PageKind.EVENT, 200, EVENT)

    assert fresh(policy, EVENT_FINAL, EVENT_FINAL + timedelta(days=400))
    assert fresh(policy, EVENT_FINAL + timedelta(days=5), EVENT_FINAL + timedelta(days=400))


def test_an_event_with_no_known_date_is_never_final():
    policy = source.cache_policy(PageKind.EVENT, 200, None)
    fetched = datetime(2030, 1, 1, tzinfo=UTC)
    assert not fresh(policy, fetched, fetched + timedelta(hours=25))


def test_other_page_kinds_and_a_404():
    robots = source.cache_policy(PageKind.ROBOTS, 200, None)
    year_list = source.cache_policy(PageKind.EVENTS_COMPLETED, 200, None)
    missing = source.cache_policy(PageKind.EVENT, 404, EVENT)
    assert robots.ttl == timedelta(hours=24)
    assert year_list.ttl == timedelta(hours=6)
    assert missing.ttl == timedelta(days=1) and missing.final_after is None
    for kind in (PageKind.FIGHT, PageKind.FIGHTER, PageKind.EVENTS_UPCOMING):
        with pytest.raises(ValueError, match="no .* pages"):
            source.cache_policy(kind, 200, None)


# -- selection and config -----------------------------------------------------------------------


def test_get_source_builds_the_wikipedia_source():
    chosen = get_source(
        ScraperSettings(scraper_user_agent=UA, scraper_source="wikipedia", _env_file=None)
    )
    assert (chosen.name, chosen.base_url) == ("wikipedia", BASE)


def test_the_real_wikipedia_gets_the_same_one_request_per_second_floor_as_ufcstats():
    with pytest.raises(ValueError, match="SCRAPER_MIN_INTERVAL_MS must be >= 1000"):
        ScraperSettings(
            scraper_user_agent=UA,
            scraper_source="wikipedia",
            scraper_min_interval_ms=100,
            _env_file=None,
        )
    assert ScraperSettings(
        scraper_user_agent=UA, scraper_source="wikipedia", scraper_min_interval_ms=1000,
        _env_file=None,
    )  # fmt: skip
    # the replay source stays exempt, as before
    assert ScraperSettings(
        scraper_user_agent=UA, scraper_source="ufcstats_replay", scraper_min_interval_ms=100,
        _env_file=None,
    )  # fmt: skip
