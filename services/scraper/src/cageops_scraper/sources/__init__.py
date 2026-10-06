from cageops_scraper.config import ScraperSettings
from cageops_scraper.sources.base import CachePolicy, PageKind, SlowResponse, Source
from cageops_scraper.sources.ufcstats import UfcStatsSource
from cageops_scraper.sources.wikipedia import WikipediaSource

__all__ = [
    "CachePolicy",
    "PageKind",
    "SlowResponse",
    "Source",
    "UfcStatsSource",
    "WikipediaSource",
    "get_source",
]


def get_source(settings: ScraperSettings) -> Source:
    """The Source named by SCRAPER_SOURCE. The replay source has its own name, so its pages,
    rate-limit key and circuit breaker never mix with real ufcstats."""
    if settings.scraper_source == "ufcstats":
        return UfcStatsSource("ufcstats", settings.ufcstats_base_url)
    if settings.scraper_source == "ufcstats_replay":
        return UfcStatsSource("ufcstats_replay", settings.ufcstats_replay_base_url)
    if settings.scraper_source == "wikipedia":
        return WikipediaSource(settings.wikipedia_base_url)
    raise ValueError(f"unknown SCRAPER_SOURCE: {settings.scraper_source!r}")
