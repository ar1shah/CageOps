from datetime import date

import httpx
import pytest
from prometheus_client import REGISTRY
from sqlalchemy import text

from cageops_scraper.errors import (
    NotFound,
    PermanentFetchError,
    RetryableFetchError,
    RobotsDisallowed,
    RobotsUnavailable,
    SourceBlocked,
)
from cageops_scraper.sources import PageKind, UfcStatsSource

FIGHT = "http://ufcstats.com/fight-details/aaaa000000000001"
FIGHTER = "http://ufcstats.com/fighter-details/bbbb000000000001"
ROBOTS = "http://ufcstats.com/robots.txt"
EVENT_DATE = date(2026, 4, 18)  # the clock starts the morning after (2026-04-19 06:00 UTC)


def counter(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, {"source": "ufcstats", **labels}) or 0.0


def raw_pages(db) -> dict[str, tuple[int, str]]:
    with db.connect() as conn:
        rows = conn.execute(text("SELECT url, status, html FROM raw_pages")).all()
    return {url: (status, html) for url, status, html in rows}


# -- cache --------------------------------------------------------------------------------


def test_a_miss_fetches_and_stores_then_the_next_call_is_a_hit_with_no_request(
    fetcher, site, db, sleeps
):
    site.add(FIGHT, "<html>fight</html>")
    misses, hits = (
        counter("scraper_cache_total", result="miss"),
        counter("scraper_cache_total", result="hit"),
    )

    first = fetcher.fetch(FIGHT, PageKind.FIGHT, EVENT_DATE)
    second = fetcher.fetch(FIGHT, PageKind.FIGHT, EVENT_DATE)

    assert (first.html, first.from_cache) == ("<html>fight</html>", False)
    assert (second.html, second.from_cache) == ("<html>fight</html>", True)
    assert site.urls == [FIGHT]  # one request in total
    assert raw_pages(db) == {FIGHT: (200, "<html>fight</html>")}
    assert counter("scraper_cache_total", result="miss") == misses + 1
    assert counter("scraper_cache_total", result="hit") == hits + 1


def test_a_cache_hit_never_touches_the_rate_limiter(fetcher, site, redis_client):
    site.add(FIGHT)
    fetcher.fetch(FIGHT, PageKind.FIGHT, EVENT_DATE)
    slot_after_first = redis_client.get(fetcher.limiter.key)

    for _ in range(5):
        fetcher.fetch(FIGHT, PageKind.FIGHT, EVENT_DATE)

    assert redis_client.get(fetcher.limiter.key) == slot_after_first


def test_an_unsettled_fight_page_goes_stale_after_six_hours(fetcher, site, clock):
    site.add(FIGHT, "<html>v1</html>")
    fetcher.fetch(FIGHT, PageKind.FIGHT, EVENT_DATE)
    clock.advance(hours=7)
    site.add(FIGHT, "<html>v2</html>")
    stale = counter("scraper_cache_total", result="stale")

    page = fetcher.fetch(FIGHT, PageKind.FIGHT, EVENT_DATE)

    assert page.html == "<html>v2</html>"
    assert len(site.requests) == 2
    assert counter("scraper_cache_total", result="stale") == stale + 1


def test_a_fight_page_fetched_after_the_event_settled_is_never_fetched_again(fetcher, site, clock):
    clock.advance(days=4)  # now 2026-04-23, more than 3 days after the event
    site.add(FIGHT, "<html>final</html>")
    fetcher.fetch(FIGHT, PageKind.FIGHT, EVENT_DATE)
    clock.advance(days=700)

    page = fetcher.fetch(FIGHT, PageKind.FIGHT, EVENT_DATE)

    assert page.from_cache and len(site.requests) == 1


def test_fighter_pages_last_thirty_days(fetcher, site, clock):
    site.add(FIGHTER)
    fetcher.fetch(FIGHTER, PageKind.FIGHTER)
    clock.advance(days=29)
    fetcher.fetch(FIGHTER, PageKind.FIGHTER)
    assert len(site.requests) == 1

    clock.advance(days=2)
    fetcher.fetch(FIGHTER, PageKind.FIGHTER)
    assert len(site.requests) == 2


def test_force_refetches_and_overwrites_the_cached_copy(fetcher, site, db):
    site.add(FIGHT, "<html>old</html>")
    fetcher.fetch(FIGHT, PageKind.FIGHT, EVENT_DATE)
    site.add(FIGHT, "<html>new</html>")
    forced = counter("scraper_cache_total", result="forced")

    page = fetcher.fetch(FIGHT, PageKind.FIGHT, EVENT_DATE, force=True)

    assert page.html == "<html>new</html>" and not page.from_cache
    assert raw_pages(db)[FIGHT] == (200, "<html>new</html>")
    assert counter("scraper_cache_total", result="forced") == forced + 1


def test_different_spellings_of_a_url_share_one_cache_row(fetcher, site, db):
    site.add(FIGHT)

    fetcher.fetch("https://www.ufcstats.com/fight-details/aaaa000000000001/", PageKind.FIGHT)
    fetcher.fetch(FIGHT, PageKind.FIGHT)

    assert len(site.requests) == 1
    assert list(raw_pages(db)) == [FIGHT]


def test_a_url_from_another_site_is_refused_before_any_request(fetcher, site):
    with pytest.raises(ValueError, match="not a ufcstats URL"):
        fetcher.fetch("http://example.com/fight-details/x", PageKind.FIGHT)

    assert site.requests == []


def test_a_cached_row_from_another_source_is_not_served(db, make_fetcher, site):
    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO raw_pages (url, source, fetched_at, status, html)"
                " VALUES (:u, 'ufcstats_replay', now(), 200, 'replay copy')"
            ),
            {"u": FIGHT},
        )
    site.add(FIGHT, "<html>real</html>")

    page = make_fetcher().fetch(FIGHT, PageKind.FIGHT, EVENT_DATE)

    assert page.html == "<html>real</html>"  # not 'replay copy'


# -- 404 and retryable errors -------------------------------------------------------------


def test_404_is_cached_and_raised_both_times_with_one_request(fetcher, site, db):
    site.add(FIGHT, "<h1>Not Found</h1>", status=404)

    for _ in range(2):
        with pytest.raises(NotFound):
            fetcher.fetch(FIGHT, PageKind.FIGHT, EVENT_DATE)

    assert len(site.requests) == 1
    assert raw_pages(db)[FIGHT][0] == 404


def test_a_cached_404_is_retried_after_a_day(fetcher, site, clock):
    site.add(FIGHT, status=404)
    with pytest.raises(NotFound):
        fetcher.fetch(FIGHT, PageKind.FIGHT, EVENT_DATE)
    clock.advance(hours=25)
    site.add(FIGHT, "<html>it exists now</html>")

    assert fetcher.fetch(FIGHT, PageKind.FIGHT, EVENT_DATE).html == "<html>it exists now</html>"


@pytest.mark.parametrize("status", [500, 502, 503])
def test_server_errors_are_retryable_and_not_cached(fetcher, site, db, status):
    site.add(FIGHT, "oops", status=status)

    with pytest.raises(RetryableFetchError):
        fetcher.fetch(FIGHT, PageKind.FIGHT, EVENT_DATE)

    assert raw_pages(db) == {}


def test_a_timeout_is_retryable_and_not_cached(fetcher, site, db):
    site.raise_on[FIGHT] = httpx.ConnectTimeout("too slow")

    with pytest.raises(RetryableFetchError, match="ConnectTimeout"):
        fetcher.fetch(FIGHT, PageKind.FIGHT, EVENT_DATE)

    assert raw_pages(db) == {}


def test_429_is_retryable_and_makes_every_worker_back_off(fetcher, site, db):
    site.add(FIGHT, "slow down", status=429, **{"Retry-After": "30"})

    with pytest.raises(RetryableFetchError, match="429"):
        fetcher.fetch(FIGHT, PageKind.FIGHT, EVENT_DATE)

    assert raw_pages(db) == {}
    assert 29_000 < fetcher.limiter.reserve(1000).wait_ms <= 30_000


def test_429_without_retry_after_backs_off_for_a_minute(fetcher, site):
    site.add(FIGHT, status=429)

    with pytest.raises(RetryableFetchError):
        fetcher.fetch(FIGHT, PageKind.FIGHT, EVENT_DATE)

    assert 59_000 < fetcher.limiter.reserve(1000).wait_ms <= 60_000


def test_other_4xx_is_permanent_and_not_cached(fetcher, site, db):
    site.add(FIGHT, status=410)

    with pytest.raises(PermanentFetchError) as gone:
        fetcher.fetch(FIGHT, PageKind.FIGHT, EVENT_DATE)

    assert gone.value.status == 410
    assert raw_pages(db) == {}


# -- redirects ----------------------------------------------------------------------------

MOVED = "http://ufcstats.com/fight-details/aaaa000000000002"
HTTPS_FIGHT = "https://ufcstats.com/fight-details/aaaa000000000001"


def test_a_same_host_redirect_is_followed_and_each_hop_takes_a_rate_limit_slot(
    fetcher, site, db, sleeps
):
    site.add(FIGHT, status=301, Location="/fight-details/aaaa000000000002")  # relative Location
    site.add(MOVED, "<html>moved here</html>")

    page = fetcher.fetch(FIGHT, PageKind.FIGHT, EVENT_DATE)

    assert page.html == "<html>moved here</html>"
    assert site.urls == [FIGHT, MOVED]
    assert len(sleeps) == 1  # two requests, so the second one waited for its own slot
    assert set(raw_pages(db)) == {FIGHT, MOVED}  # asking for the old URL again is a cache hit


def test_after_a_redirect_asking_for_the_old_url_again_sends_no_request(fetcher, site):
    site.add(FIGHT, status=301, Location=MOVED)
    site.add(MOVED, "<html>moved here</html>")
    fetcher.fetch(FIGHT, PageKind.FIGHT, EVENT_DATE)

    again = fetcher.fetch(FIGHT, PageKind.FIGHT, EVENT_DATE)

    assert again.from_cache and again.html == "<html>moved here</html>"
    assert len(site.requests) == 2


def test_an_http_to_https_redirect_is_really_requested_over_https(fetcher, site, db):
    site.add(FIGHT, status=301, Location=HTTPS_FIGHT)
    site.add(HTTPS_FIGHT, "<html>secure</html>")

    page = fetcher.fetch(FIGHT, PageKind.FIGHT, EVENT_DATE)

    assert page.html == "<html>secure</html>"
    assert site.urls == [FIGHT, HTTPS_FIGHT]
    assert list(raw_pages(db)) == [FIGHT]  # one cache row: the canonical spelling


def test_an_https_to_http_redirect_is_followed_too(make_fetcher, site):
    secure_source = UfcStatsSource("ufcstats", "https://ufcstats.com")
    fetcher = make_fetcher(source=secure_source)
    site.add(HTTPS_FIGHT, status=302, Location=FIGHT)
    site.add(FIGHT, "<html>plain</html>")

    page = fetcher.fetch(HTTPS_FIGHT, PageKind.FIGHT, EVENT_DATE)

    assert page.html == "<html>plain</html>"
    assert site.urls == [HTTPS_FIGHT, FIGHT]


def test_a_redirect_to_another_site_is_a_permanent_error_and_nothing_is_cached(fetcher, site, db):
    site.add(FIGHTER, status=301, Location="http://elsewhere.example/x")

    with pytest.raises(PermanentFetchError, match="cross-host redirect") as moved:
        fetcher.fetch(FIGHTER, PageKind.FIGHTER)

    assert moved.value.status == 301
    assert site.urls == [FIGHTER]  # the other site was never contacted
    assert raw_pages(db) == {}


def test_a_redirect_gives_up_after_three_hops(fetcher, site):
    chain = [f"http://ufcstats.com/fight-details/hop{n}" for n in range(6)]
    for here, there in zip(chain, chain[1:], strict=False):
        site.add(here, status=301, Location=there)

    with pytest.raises(PermanentFetchError, match="too many redirects"):
        fetcher.fetch(chain[0], PageKind.FIGHT, EVENT_DATE)

    assert site.urls == chain[:4]  # the original request plus three followed hops


def test_three_hops_is_still_allowed(fetcher, site):
    chain = [f"http://ufcstats.com/fight-details/hop{n}" for n in range(4)]
    for here, there in zip(chain, chain[1:], strict=False):
        site.add(here, status=301, Location=there)
    site.pages[chain[-1]] = (200, "<html>end</html>", {})

    assert fetcher.fetch(chain[0], PageKind.FIGHT, EVENT_DATE).html == "<html>end</html>"


def test_a_redirect_loop_ends_at_the_hop_limit(fetcher, site):
    other = "http://ufcstats.com/fight-details/loop"
    site.add(FIGHT, status=301, Location=other)
    site.add(other, status=301, Location=FIGHT)

    with pytest.raises(PermanentFetchError, match="too many redirects"):
        fetcher.fetch(FIGHT, PageKind.FIGHT, EVENT_DATE)

    assert len(site.requests) == 4


def test_a_redirect_without_a_location_is_permanent(fetcher, site):
    site.add(FIGHT, status=301)

    with pytest.raises(PermanentFetchError, match="unfollowable"):
        fetcher.fetch(FIGHT, PageKind.FIGHT, EVENT_DATE)


def test_a_challenge_page_behind_a_redirect_still_trips_the_breaker(
    fetcher, site, db, challenge_html
):
    site.add(FIGHT, status=301, Location=MOVED)
    site.add(MOVED, challenge_html)

    with pytest.raises(SourceBlocked):
        fetcher.fetch(FIGHT, PageKind.FIGHT, EVENT_DATE)

    assert fetcher.breaker.state() is not None
    assert raw_pages(db) == {}
    with pytest.raises(SourceBlocked):
        fetcher.fetch(FIGHTER, PageKind.FIGHTER)
    assert len(site.requests) == 2  # nothing more was sent


def test_every_redirect_logs_its_location(fetcher, site, caplog):
    site.add(FIGHT, status=301, Location=MOVED)
    site.add(MOVED)

    with caplog.at_level("INFO", logger="cageops_scraper.fetch"):
        fetcher.fetch(FIGHT, PageKind.FIGHT, EVENT_DATE)

    redirects = [r for r in caplog.records if r.getMessage() == "redirect"]
    assert [(r.status, r.location, r.hop) for r in redirects] == [(301, MOVED, 1)]


# -- politeness ---------------------------------------------------------------------------


def test_every_request_carries_our_user_agent(fetcher, site):
    site.add(FIGHT)

    fetcher.fetch(FIGHT, PageKind.FIGHT, EVENT_DATE)

    assert site.requests[0].headers["user-agent"].startswith("CageOps/0.1 (+https://github.com/")


def test_back_to_back_cold_fetches_wait_for_their_slot(fetcher, site, sleeps):
    site.add(FIGHT)
    site.add(FIGHTER)

    fetcher.fetch(FIGHT, PageKind.FIGHT, EVENT_DATE)
    fetcher.fetch(FIGHTER, PageKind.FIGHTER)

    assert len(sleeps) == 1  # the first needed no wait, the second waits for its slot
    assert 0 < sleeps[0] <= 1.0


# -- circuit breaker ----------------------------------------------------------------------


def test_a_challenge_page_trips_the_breaker_and_is_never_cached(fetcher, site, db, challenge_html):
    site.add(FIGHT, challenge_html)  # served with 200, like the real site did
    blocked = counter("scraper_source_blocked_total", reason="browser_challenge")

    with pytest.raises(SourceBlocked, match="browser_challenge"):
        fetcher.fetch(FIGHT, PageKind.FIGHT, EVENT_DATE)

    state = fetcher.breaker.state()
    assert state is not None and state.reason == "browser_challenge" and state.url == FIGHT
    assert raw_pages(db) == {}
    assert counter("scraper_source_blocked_total", reason="browser_challenge") == blocked + 1


def test_once_tripped_no_request_is_sent_for_any_page(fetcher, site, challenge_html):
    site.add(FIGHT, challenge_html)
    site.add(FIGHTER)
    with pytest.raises(SourceBlocked):
        fetcher.fetch(FIGHT, PageKind.FIGHT, EVENT_DATE)
    sent = len(site.requests)

    for _ in range(3):
        with pytest.raises(SourceBlocked):
            fetcher.fetch(FIGHTER, PageKind.FIGHTER)

    assert len(site.requests) == sent


def test_force_does_not_get_past_an_open_breaker(fetcher, site, challenge_html):
    site.add(FIGHT, challenge_html)
    with pytest.raises(SourceBlocked):
        fetcher.fetch(FIGHT, PageKind.FIGHT, EVENT_DATE)
    sent = len(site.requests)

    with pytest.raises(SourceBlocked):
        fetcher.fetch(FIGHT, PageKind.FIGHT, EVENT_DATE, force=True)

    assert len(site.requests) == sent


def test_a_403_also_trips_the_breaker(fetcher, site):
    site.add(FIGHT, "Forbidden", status=403)

    with pytest.raises(SourceBlocked, match="http_403"):
        fetcher.fetch(FIGHT, PageKind.FIGHT, EVENT_DATE)


def test_a_worker_waiting_for_its_slot_sends_nothing_if_another_worker_tripped_the_breaker(
    make_fetcher, site, clock
):
    """Worker B is queued behind worker A's slot. While B sleeps, A hits the challenge and trips
    the breaker. B must not send its request when it wakes up."""
    site.add(FIGHT)
    site.add(FIGHTER)

    def sleep_while_a_trips_the_breaker(_seconds: float) -> None:
        other_worker.breaker.trip("browser_challenge", "http://ufcstats.com/other", clock())

    worker_b = make_fetcher(sleep=sleep_while_a_trips_the_breaker)
    other_worker = make_fetcher()
    worker_b.fetch(FIGHT, PageKind.FIGHT, EVENT_DATE)  # takes the first slot, no wait
    sent = len(site.requests)

    with pytest.raises(SourceBlocked):
        worker_b.fetch(FIGHTER, PageKind.FIGHTER)  # has to wait, so the sleep trips the breaker

    assert len(site.requests) == sent


def test_the_breaker_stays_open_until_a_human_resets_it(fetcher, site, challenge_html, clock):
    site.add(FIGHT, challenge_html)
    with pytest.raises(SourceBlocked):
        fetcher.fetch(FIGHT, PageKind.FIGHT, EVENT_DATE)
    clock.advance(days=30)  # no timer closes it

    with pytest.raises(SourceBlocked):
        fetcher.fetch(FIGHTER, PageKind.FIGHTER)

    assert fetcher.breaker.reset() is True
    site.add(FIGHTER, "<html>fighter</html>")
    assert fetcher.fetch(FIGHTER, PageKind.FIGHTER).html == "<html>fighter</html>"
    assert fetcher.breaker.reset() is False  # nothing left to reset


def test_the_real_site_and_the_replay_site_have_separate_breakers(
    fetcher, make_fetcher, site, challenge_html
):
    replay = make_fetcher(source=UfcStatsSource("ufcstats_replay", "http://127.0.0.1:8099"))
    site.add(FIGHT, challenge_html)
    with pytest.raises(SourceBlocked):
        fetcher.fetch(FIGHT, PageKind.FIGHT, EVENT_DATE)
    replay_url = "http://127.0.0.1:8099/fight-details/aaaa000000000001"
    site.add(replay_url, "<html>replay</html>")

    assert replay.fetch(replay_url, PageKind.FIGHT, EVENT_DATE).html == "<html>replay</html>"


# -- preflight (robots.txt on startup) ----------------------------------------------------


def test_preflight_with_no_robots_rules_keeps_our_default_interval(fetcher, site):
    site.add(ROBOTS, "<h1>Not Found</h1>", status=404)

    result = fetcher.preflight()

    assert (result.outcome, fetcher.interval_ms) == ("no_rules", 1000)


def test_preflight_adopts_a_stricter_crawl_delay(fetcher, site, sleeps):
    site.add(ROBOTS, "User-agent: *\nCrawl-delay: 5\n")
    site.add(FIGHT)
    site.add(FIGHTER)

    fetcher.preflight()
    fetcher.fetch(FIGHT, PageKind.FIGHT, EVENT_DATE)
    fetcher.fetch(FIGHTER, PageKind.FIGHTER)

    assert fetcher.interval_ms == 5000
    # Fake sleeps don't pass time, so the second fetch's wait exceeds the first's by exactly the
    # Crawl-delay: its slot is 5 s behind the fight's slot (the robots fetch used a 1 s gap).
    assert 4.9 < sleeps[-1] - sleeps[-2] < 5.1


def test_preflight_refuses_to_run_if_a_needed_path_is_disallowed(fetcher, site):
    site.add(ROBOTS, "User-agent: *\nDisallow: /fighter-details/\n")

    with pytest.raises(RobotsDisallowed, match="/fighter-details/"):
        fetcher.preflight()


def test_preflight_refuses_to_run_if_robots_is_unreachable(fetcher, site):
    site.add(ROBOTS, "down", status=503)

    with pytest.raises(RobotsUnavailable):
        fetcher.preflight()


def test_preflight_sends_nothing_while_the_breaker_is_open(fetcher, site, clock):
    fetcher.breaker.trip("browser_challenge", FIGHT, clock())

    with pytest.raises(SourceBlocked):
        fetcher.preflight()

    assert site.requests == []


def test_robots_txt_is_cached_for_a_day(fetcher, site, clock):
    site.add(ROBOTS, status=404)

    fetcher.preflight()
    fetcher.preflight()
    assert len(site.requests) == 1

    clock.advance(hours=25)
    fetcher.preflight()
    assert len(site.requests) == 2
