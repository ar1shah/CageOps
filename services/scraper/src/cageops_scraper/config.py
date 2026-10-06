"""Scraper settings, read from environment variables (see .env.example).

Kept separate from cageops_common's Settings so the API never needs scraper variables.
"""

from functools import lru_cache
from typing import Self
from urllib.parse import urlsplit

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# CLAUDE.md rule 2: at most ~1 request/second against the real site.
REAL_SITE_MIN_INTERVAL_MS = 1000
# Sources that talk to a real site (the replay source is excluded on purpose).
REAL_SITES = ("ufcstats", "wikipedia")
PLACEHOLDER_CONTACT = "you@example.com"
# The replay source may run faster than the real site (benchmarks), so it must be impossible to
# point it at the real site. It only ever talks to this machine.
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


class ScraperSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Required: no default, so the scraper refuses to start without a contact for site operators.
    scraper_user_agent: str
    scraper_min_interval_ms: int = REAL_SITE_MIN_INTERVAL_MS
    scraper_timeout_s: float = 20.0
    # Which Source to use. "ufcstats_replay" is the local replay server used for benchmarks;
    # it has its own name so its pages never mix with real ufcstats rows, limiter or breaker.
    scraper_source: str = "ufcstats"
    ufcstats_base_url: str = "http://ufcstats.com"
    ufcstats_replay_base_url: str = "http://127.0.0.1:8099"
    wikipedia_base_url: str = "https://en.wikipedia.org"

    @model_validator(mode="after")
    def _polite_defaults(self) -> Self:
        if PLACEHOLDER_CONTACT in self.scraper_user_agent:
            raise ValueError(
                "SCRAPER_USER_AGENT still has the placeholder contact; put your real email in it"
            )
        if (
            self.scraper_source in REAL_SITES
            and self.scraper_min_interval_ms < REAL_SITE_MIN_INTERVAL_MS
        ):
            raise ValueError(
                f"SCRAPER_MIN_INTERVAL_MS must be >= {REAL_SITE_MIN_INTERVAL_MS} for the real site"
            )
        host = urlsplit(self.ufcstats_replay_base_url).hostname
        if host not in LOOPBACK_HOSTS:
            raise ValueError(
                "UFCSTATS_REPLAY_BASE_URL must point at this machine "
                f"({', '.join(LOOPBACK_HOSTS)}), not {host!r}: the replay source skips the real "
                "site's 1 request/second floor, so it must never be able to reach the real site"
            )
        return self


@lru_cache
def get_scraper_settings() -> ScraperSettings:
    return ScraperSettings()  # type: ignore[call-arg]  # fields come from the environment
