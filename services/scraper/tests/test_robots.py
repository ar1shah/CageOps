from datetime import UTC, datetime

import pytest

from cageops_scraper.errors import (
    NotFound,
    PermanentFetchError,
    RetryableFetchError,
    RobotsDisallowed,
    RobotsUnavailable,
)
from cageops_scraper.page import Page
from cageops_scraper.robots import check_robots, crawl_delay
from cageops_scraper.sources import UfcStatsSource

UA = "CageOps/0.1 (+https://github.com/ar1shah/cageops; test@cageops.dev)"
SOURCE = UfcStatsSource()


def _page(body: str) -> Page:
    return Page("http://ufcstats.com/robots.txt", 200, body, datetime.now(UTC), False)


def _check(body_or_error):
    def fetch(url: str) -> Page:
        assert url == "http://ufcstats.com/robots.txt"
        if isinstance(body_or_error, Exception):
            raise body_or_error
        return _page(body_or_error)

    return check_robots(SOURCE, UA, fetch, default_interval_ms=1000)


def test_404_means_no_rules_and_our_own_limit_applies():
    result = _check(NotFound("http://ufcstats.com/robots.txt"))

    assert result.outcome == "no_rules"
    assert result.interval_ms == 1000


def test_other_4xx_also_means_no_rules():
    assert _check(PermanentFetchError("gone", status=410)).outcome == "no_rules"


def test_a_redirect_is_not_treated_as_no_rules():
    with pytest.raises(RobotsUnavailable):
        _check(PermanentFetchError("redirect", status=301))


def test_server_error_means_refuse_to_run():
    with pytest.raises(RobotsUnavailable):
        _check(RetryableFetchError("503"))


def test_allow_all_file_passes():
    result = _check("User-agent: *\nDisallow:\n")

    assert result.outcome == "parsed"
    assert result.interval_ms == 1000


def test_disallowing_a_path_we_need_refuses_to_run_and_names_it():
    with pytest.raises(RobotsDisallowed) as excinfo:
        _check("User-agent: *\nDisallow: /fight-details/\n")

    assert excinfo.value.path == "/fight-details/"


def test_disallowing_everything_for_us_refuses_to_run():
    with pytest.raises(RobotsDisallowed):
        _check("User-agent: cageops\nDisallow: /\n")


def test_disallowing_only_other_bots_is_fine():
    assert (
        _check("User-agent: GPTBot\nDisallow: /\n\nUser-agent: *\nDisallow:\n").interval_ms == 1000
    )


def test_a_stricter_crawl_delay_overrides_our_default():
    result = _check("User-agent: *\nCrawl-delay: 5\n")

    assert result.crawl_delay_s == 5
    assert result.interval_ms == 5000


def test_a_looser_crawl_delay_does_not_speed_us_up():
    result = _check("User-agent: *\nCrawl-delay: 0.5\n")

    assert result.crawl_delay_s == 0.5
    assert result.interval_ms == 1000


def test_fractional_crawl_delay_is_honored():
    assert _check("User-agent: *\nCrawl-delay: 1.5\n").interval_ms == 1500


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("User-agent: *\nCrawl-delay: 10\n", 10),
        # the group for our own name wins over "*" and doesn't inherit from it
        ("User-agent: cageops\nCrawl-delay: 2\n\nUser-agent: *\nCrawl-delay: 30\n", 2),
        ("User-agent: cageops\nDisallow:\n\nUser-agent: *\nCrawl-delay: 30\n", None),
        # another bot's delay is not ours
        ("User-agent: otherbot\nCrawl-delay: 30\n", None),
        # several user-agent lines share one group
        ("User-agent: otherbot\nUser-agent: cageops\nCrawl-delay: 4\n", 4),
        ("User-agent: *\nCrawl-delay: soon\n", None),
        ("# nothing here\n", None),
    ],
)
def test_crawl_delay_group_rules(body, expected):
    assert crawl_delay(body, UA) == expected
