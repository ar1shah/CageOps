"""After a slow response the source's pause is applied to EVERY worker (Robot policy: "if a request
takes more than 1 second to serve, wait 5 seconds before making another"). No test really waits:
the fetcher's timer and sleep are fakes, and the limiter is real Redis."""

import pytest

from cageops_scraper.sources import PageKind, UfcStatsSource, WikipediaSource

ONE = "https://en.wikipedia.org/wiki/UFC_332"
TWO = "https://en.wikipedia.org/wiki/UFC_331"
UFC = "http://ufcstats.com/fight-details/aaaa000000000001"


class FakeTimer:
    """Each live request reads the timer twice (start, end); request N takes durations[N]."""

    def __init__(self, *durations: float):
        self.durations = list(durations)
        self.now = 0.0
        self.reads = 0

    def __call__(self) -> float:
        self.reads += 1
        if self.reads % 2 == 0:  # the end of a request
            self.now += self.durations.pop(0) if self.durations else 0.0
        return self.now


@pytest.fixture
def wiki(make_fetcher, site):
    for url in (ONE, TWO):
        site.add(url)

    def build(*durations, **overrides):
        return make_fetcher(source=WikipediaSource(), timer=FakeTimer(*durations), **overrides)

    return build


def test_a_response_over_a_second_makes_the_next_request_wait_five_seconds(wiki, sleeps):
    fetcher = wiki(1.5)

    fetcher.fetch(ONE, PageKind.EVENT)
    fetcher.fetch(TWO, PageKind.EVENT)

    assert len(sleeps) == 1 and 4.5 < sleeps[0] <= 5.0


def test_a_fast_response_waits_only_the_normal_interval(wiki, sleeps):
    fetcher = wiki(0.2)

    fetcher.fetch(ONE, PageKind.EVENT)
    fetcher.fetch(TWO, PageKind.EVENT)

    assert len(sleeps) == 1 and 0 < sleeps[0] <= 1.0


def test_exactly_one_second_is_not_slow(wiki, sleeps):
    fetcher = wiki(1.0)

    fetcher.fetch(ONE, PageKind.EVENT)
    fetcher.fetch(TWO, PageKind.EVENT)

    assert sleeps[0] <= 1.0


def test_the_pause_is_shared_by_every_worker_of_the_source(wiki, sleeps):
    slow_worker, other_worker = wiki(2.0), wiki(0.1)

    slow_worker.fetch(ONE, PageKind.EVENT)
    other_worker.fetch(TWO, PageKind.EVENT)  # a different fetcher, the same source and Redis key

    assert len(sleeps) == 1 and 4.5 < sleeps[0] <= 5.0


def test_a_slow_error_response_pauses_too(wiki, site, sleeps):
    from cageops_scraper.errors import RetryableFetchError

    site.add(ONE, status=503)
    fetcher = wiki(3.0)

    with pytest.raises(RetryableFetchError):
        fetcher.fetch(ONE, PageKind.EVENT)
    fetcher.fetch(TWO, PageKind.EVENT)

    assert 4.5 < sleeps[0] <= 5.0


def test_a_cache_hit_sends_no_request_and_so_pauses_nobody(wiki, sleeps):
    fetcher = wiki(5.0)
    fetcher.fetch(ONE, PageKind.EVENT)
    sleeps.clear()

    fetcher.fetch(ONE, PageKind.EVENT)  # served from raw_pages

    assert sleeps == []


def test_ufcstats_has_no_such_rule_so_a_slow_response_pauses_nothing(make_fetcher, site, sleeps):
    site.add(UFC)
    site.add("http://ufcstats.com/fight-details/bbbb000000000002")
    fetcher = make_fetcher(source=UfcStatsSource(), timer=FakeTimer(30.0))

    fetcher.fetch(UFC, PageKind.FIGHT)
    fetcher.fetch("http://ufcstats.com/fight-details/bbbb000000000002", PageKind.FIGHT)

    assert sleeps[0] <= 1.0
