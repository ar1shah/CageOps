"""The one place that makes HTTP requests. Every job goes through Fetcher.fetch().

The path of one request:

    canonical URL -> cache (fresh? return, no request) -> breaker open? stop
        -> wait for a rate-limit slot -> breaker open? stop (someone tripped it while we waited)
        -> GET -> classify the answer (cache it, retry it, or trip the breaker)

Cache hits return before the rate limiter, so a run served from cache never waits on it.
A same-site redirect is followed by running that whole path again on the Location URL (so every
hop is cached, checked against the breaker and takes its own rate-limit slot), up to
MAX_REDIRECTS hops. A redirect to another site is a permanent error.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from urllib.parse import urljoin

import httpx
from redis import Redis
from sqlalchemy import Engine

from cageops_common.logs import log_context
from cageops_scraper.breaker import CircuitBreaker
from cageops_scraper.cache import PageCache
from cageops_scraper.config import ScraperSettings
from cageops_scraper.errors import (
    NotFound,
    PermanentFetchError,
    RetryableFetchError,
    SourceBlocked,
)
from cageops_scraper.metrics import CACHE_TOTAL, FETCH_SECONDS, SOURCE_BLOCKED_TOTAL
from cageops_scraper.page import Page
from cageops_scraper.ratelimit import RateLimiter
from cageops_scraper.robots import RobotsResult, check_robots
from cageops_scraper.sources.base import PageKind, Source

log = logging.getLogger(__name__)

DEFAULT_RETRY_AFTER_S = 60
MAX_RETRY_AFTER_S = 3600
MAX_REDIRECTS = 3
REDIRECT_STATUSES = {301, 302, 303, 307, 308}


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Fetcher:
    def __init__(
        self,
        source: Source,
        engine: Engine,
        redis: Redis,
        client: httpx.Client,
        settings: ScraperSettings,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = _utcnow,
    ):
        self.source = source
        self.user_agent = settings.scraper_user_agent
        self.interval_ms = settings.scraper_min_interval_ms
        self._default_interval_ms = settings.scraper_min_interval_ms
        self._timeout_s = settings.scraper_timeout_s
        self._client = client
        self._clock = clock
        self.cache = PageCache(engine, source.name)
        self.limiter = RateLimiter(redis, source.name, sleep=sleep)
        self.breaker = CircuitBreaker(redis, source.name)

    def preflight(self) -> RobotsResult:
        """Run at every scraper start: refuse if the breaker is open or robots.txt disallows a
        path we need, and adopt a stricter Crawl-delay if there is one."""
        self._raise_if_blocked()
        result = check_robots(
            self.source,
            self.user_agent,
            lambda url: self.fetch(url, PageKind.ROBOTS),
            self._default_interval_ms,
        )
        self.interval_ms = result.interval_ms
        log.info(
            "robots.txt checked",
            extra={
                "source": self.source.name,
                "outcome": result.outcome,
                "crawl_delay_s": result.crawl_delay_s,
                "interval_ms": result.interval_ms,
            },
        )
        return result

    def fetch(
        self,
        url: str,
        kind: PageKind,
        event_date: date | None = None,
        force: bool = False,
    ) -> Page:
        """The page at `url`, from cache if a fresh copy exists, else from the site.

        event_date is the date of the event the page belongs to (it decides when a fight or
        event page stops changing). force=True skips the cache read but still obeys the circuit
        breaker and the rate limiter, and stores the new copy.
        """
        # The first request goes to the canonical spelling. A redirect hop goes to the Location
        # exactly as given (so an http -> https hop really is requested over https), while its
        # cache entry is still filed under the canonical URL.
        return self._fetch(self.source.canonical_url(url), kind, event_date, force, hops=0)

    def _fetch(
        self, request_url: str, kind: PageKind, event_date: date | None, force: bool, hops: int
    ) -> Page:
        key = self.source.canonical_url(request_url)
        with log_context(url=key):
            cached = self._read_cache(key, kind, event_date, force)
            if cached is not None:
                return cached
            return self._fetch_live(request_url, key, kind, event_date, force, hops)

    # -- cache ---------------------------------------------------------------------------

    def _read_cache(
        self, url: str, kind: PageKind, event_date: date | None, force: bool
    ) -> Page | None:
        name = self.source.name
        if force:
            CACHE_TOTAL.labels(name, "forced").inc()
            return None
        entry = self.cache.get(url)
        if entry is None:
            CACHE_TOTAL.labels(name, "miss").inc()
            return None
        policy = self.source.cache_policy(kind, entry.status, event_date)
        if not policy.is_fresh(entry.fetched_at, self._clock()):
            CACHE_TOTAL.labels(name, "stale").inc()
            return None
        CACHE_TOTAL.labels(name, "hit").inc()
        if entry.status == 404:
            raise NotFound(url)
        return Page(url, entry.status, entry.html, entry.fetched_at, from_cache=True)

    # -- live request --------------------------------------------------------------------

    def _fetch_live(
        self,
        request_url: str,
        key: str,
        kind: PageKind,
        event_date: date | None,
        force: bool,
        hops: int,
    ) -> Page:
        self._raise_if_blocked()
        self.limiter.acquire(self.interval_ms)
        self._raise_if_blocked()  # another worker may have tripped it while we waited

        response = self._get(request_url)
        status = response.status_code
        html = response.text

        reason = self.source.block_reason(status, html)
        if reason is not None:
            self._trip(reason, request_url)
            raise SourceBlocked(reason, request_url)  # never cached, never retried
        if status == 200:
            return self._store(key, status, html)
        if status == 404:
            self._store(key, status, html)
            raise NotFound(key)
        if status == 429:
            wait = self._retry_after(response)
            self.limiter.penalize(wait)  # everyone backs off, not just this worker
            raise RetryableFetchError(f"429 from {request_url}; backing off {wait:.0f}s")
        if 500 <= status < 600:
            raise RetryableFetchError(f"{status} from {request_url}")
        if 300 <= status < 400:
            return self._follow_redirect(response, request_url, key, kind, event_date, force, hops)
        raise PermanentFetchError(f"{status} from {request_url}", status=status)

    def _follow_redirect(
        self,
        response: httpx.Response,
        request_url: str,
        key: str,
        kind: PageKind,
        event_date: date | None,
        force: bool,
        hops: int,
    ) -> Page:
        status = response.status_code
        location = response.headers.get("location")
        log.info("redirect", extra={"status": status, "location": location, "hop": hops + 1})
        if status not in REDIRECT_STATUSES or not location:
            raise PermanentFetchError(f"unfollowable {status} from {request_url}", status=status)
        if hops >= MAX_REDIRECTS:
            raise PermanentFetchError(
                f"too many redirects (more than {MAX_REDIRECTS}) from {request_url}", status=status
            )
        target = urljoin(request_url, location)  # Location may be relative
        try:
            self.source.canonical_url(target)  # raises if it is not this site
        except ValueError as exc:
            raise PermanentFetchError(
                f"cross-host redirect from {request_url} to {target}", status=status
            ) from exc

        page = self._fetch(target, kind, event_date, force, hops + 1)
        if page.url != key:
            # File the answer under the URL that was asked for too, so asking again is a cache
            # hit instead of another redirect round trip.
            self.cache.put(key, page.status, page.html, page.fetched_at)
        return page

    def _get(self, url: str) -> httpx.Response:
        name = self.source.name
        started = time.perf_counter()
        try:
            response = self._client.get(
                url,
                headers={"User-Agent": self.user_agent},
                timeout=self._timeout_s,
                follow_redirects=False,  # we follow hops ourselves, through the cache and limiter
            )
        except httpx.HTTPError as exc:  # timeouts, connection resets, DNS failures, ...
            FETCH_SECONDS.labels(name, "error").observe(time.perf_counter() - started)
            raise RetryableFetchError(f"{type(exc).__name__} fetching {url}: {exc}") from exc
        elapsed = time.perf_counter() - started
        FETCH_SECONDS.labels(name, f"{response.status_code // 100}xx").observe(elapsed)
        log.info(
            "fetched",
            extra={"status": response.status_code, "elapsed_ms": round(elapsed * 1000)},
        )
        return response

    def _store(self, url: str, status: int, html: str) -> Page:
        fetched_at = self._clock()
        self.cache.put(url, status, html, fetched_at)
        return Page(url, status, html, fetched_at, from_cache=False)

    # -- circuit breaker -----------------------------------------------------------------

    def _raise_if_blocked(self) -> None:
        state = self.breaker.state()
        if state is not None:
            raise SourceBlocked(state.reason, state.url)

    def _trip(self, reason: str, url: str) -> None:
        SOURCE_BLOCKED_TOTAL.labels(self.source.name, reason).inc()
        if self.breaker.trip(reason, url, self._clock()):
            log.error(
                "source is blocking automated clients; circuit breaker opened, all fetching stops",
                extra={"source": self.source.name, "reason": reason},
            )

    @staticmethod
    def _retry_after(response: httpx.Response) -> float:
        raw = response.headers.get("retry-after", "")
        if raw.strip().isdigit():
            return min(float(raw), MAX_RETRY_AFTER_S)
        return DEFAULT_RETRY_AFTER_S  # missing, or an HTTP-date we don't parse
