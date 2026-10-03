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
