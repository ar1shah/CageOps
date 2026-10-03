import pytest
from pydantic import ValidationError

from cageops_scraper.config import ScraperSettings

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


def test_env_variables_are_read(monkeypatch):
    monkeypatch.setenv("SCRAPER_USER_AGENT", UA)
    monkeypatch.setenv("SCRAPER_TIMEOUT_S", "7")

    settings = make()

    assert settings.scraper_user_agent == UA
    assert settings.scraper_timeout_s == 7
