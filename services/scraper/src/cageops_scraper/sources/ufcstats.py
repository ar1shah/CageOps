"""ufcstats.com as a Source (and the local replay server that imitates it for benchmarks)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from urllib.parse import urlsplit, urlunsplit

from cageops_scraper.sources.base import CachePolicy, PageKind

REAL_HOST = "ufcstats.com"

# A fight or event page can still change until a few days after the event (results post on
# fight night, stats are sometimes corrected the next day). After that it is treated as final.
FINAL_AFTER_EVENT = timedelta(days=3)
UNSETTLED_TTL = timedelta(hours=6)

# Markers of the proof-of-work page ufcstats started serving on 2026-10-03 (D-013).
_CHALLENGE_MARKERS = ("Checking your browser", '"/__c"')


class UfcStatsSource:
    required_paths = (
        "/statistics/events/completed",
        "/statistics/events/upcoming",
        "/event-details/",
        "/fight-details/",
        "/fighter-details/",
    )
    slow_response = None  # ufcstats states no such rule

    def __init__(self, name: str = "ufcstats", base_url: str = f"http://{REAL_HOST}"):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self._parts = urlsplit(self.base_url)
        # Real rows must come from the real site and replay rows must not, otherwise benchmark
        # data would be filed as live data (or the other way around).
        is_real_host = self._host(self._parts.netloc) == REAL_HOST
        if (name == "ufcstats") != is_real_host:
            raise ValueError(f"source {name!r} cannot use base URL {base_url!r}")

    @staticmethod
    def _host(netloc: str) -> str:
        return netloc.lower().removeprefix("www.")

    def canonical_url(self, url: str) -> str:
        parts = urlsplit(url.strip())
        if self._host(parts.netloc) != self._host(self._parts.netloc):
            raise ValueError(f"{url!r} is not a {self.name} URL")
        path = parts.path.rstrip("/") or "/"
        # Scheme and host come from the configured base, so https:// and www. spellings collapse.
        return urlunsplit((self._parts.scheme, self._parts.netloc, path, parts.query, ""))

    def block_reason(self, status: int, html: str) -> str | None:
        if status == 403:
            return "http_403"
        if any(marker in html for marker in _CHALLENGE_MARKERS):
            return "browser_challenge"
        return None

    def cache_policy(self, kind: PageKind, status: int, event_date: date | None) -> CachePolicy:
        if status == 404:
            return CachePolicy(ttl=timedelta(days=1))  # the page may appear later
        match kind:
            case PageKind.ROBOTS:
                return CachePolicy(ttl=timedelta(hours=24))  # RFC 9309's maximum
            case PageKind.EVENTS_COMPLETED:
                return CachePolicy(ttl=timedelta(hours=12))
            case PageKind.EVENTS_UPCOMING:
                return CachePolicy(ttl=UNSETTLED_TTL)
            case PageKind.FIGHTER:
                # We keep only bio fields (height, reach, stance, DOB), which rarely change.
                return CachePolicy(ttl=timedelta(days=30))
            case PageKind.EVENT | PageKind.FIGHT:
                if event_date is None:
                    return CachePolicy(ttl=UNSETTLED_TTL)  # unknown date: assume it can change
                settled = datetime.combine(event_date + FINAL_AFTER_EVENT, time.min, UTC)
                return CachePolicy(ttl=UNSETTLED_TTL, final_after=settled)
        raise ValueError(f"unknown page kind: {kind}")
