"""The Redis prediction cache (D-042). It can make a request faster and can never make it fail.

THE KEY is the whole argument. A cached prediction is only valid for the exact thing that
produced it, so everything the answer depended on is in the key:

    pred:v1:{model_version}:{features_build_id}:{inputs_sha12}:{lo}-{hi}:{wc}:{rounds}:{title}:
            top{n}:asof={YYYY-MM-DD}:src={adhoc}

    v1                a cache schema version: bump it when PredictionCore's shape changes
    model_version     a new model must not be served the old model's answers
    features_build_id a rebuild changes every fighter's numbers
    inputs_sha12      a result ingested since the build changes them too, before any rebuild
    lo-hi             smaller fighter id first: A-vs-B and B-vs-A are one entry (the value is
                      stored oriented lo -> hi and flipped on read, exactly)
    wc/rounds/title   the matchup's context, as REQUESTED ("auto" when the caller did not say; the
                      default is a function of data the other components already cover)
    top{n}            how many contributions the entry holds
    asof=<date>       age and days-since-last-fight depend on the date. The real date, never the
                      word "today": an entry written at 23:59 must not be served at 00:01

THE VALUE is the prediction core as JSON, not the HTTP response.

FAILING OPEN: every Redis call has a short timeout and no retries; any failure is counted and the
caller computes the answer itself. After a failure Redis is not asked again for a few seconds, so a
dead Redis does not charge every request the timeout.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from datetime import date

import redis

from cageops_api.metrics import ApiMetrics
from cageops_api.predict import PredictionCore

log = logging.getLogger(__name__)

CACHE_SCHEMA = "v1"
KEY_PREFIX = f"pred:{CACHE_SCHEMA}:"
SOURCE_ADHOC = "adhoc"

# What counts as "Redis is unavailable". OSError covers a socket that dies below redis-py.
FAIL_OPEN = (redis.exceptions.RedisError, OSError)


def slug(value: str | None) -> str:
    if value is None:
        return "auto"
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "auto"


def cache_key(
    *,
    model_version: str,
    build_id: int,
    inputs_sha12: str,
    fighter_a_id: int,
    fighter_b_id: int,
    weight_class: str | None,
    scheduled_rounds: int,
    is_title_fight: bool,
    top_n: int,
    as_of: date,
) -> str:
    lo, hi = sorted((fighter_a_id, fighter_b_id))
    return (
        f"{KEY_PREFIX}{model_version}:{build_id}:{inputs_sha12}:{lo}-{hi}:{slug(weight_class)}:"
        f"{scheduled_rounds}:{int(is_title_fight)}:top{top_n}:asof={as_of.isoformat()}:"
        f"src={SOURCE_ADHOC}"
    )


class PredictionCache:
    def __init__(
        self,
        client: redis.Redis | None,
        *,
        ttl_s: int,
        metrics: ApiMetrics,
        clock: Callable[[], float] = time.monotonic,
        skip_s: float = 5.0,
        log_every_s: float = 30.0,
    ) -> None:
        self.client = client
        self.ttl_s = ttl_s
        self.metrics = metrics
        self._clock = clock
        self._skip_s = skip_s
        self._log_every_s = log_every_s
        self._skip_until = 0.0
        self._last_logged = float("-inf")

    @property
    def enabled(self) -> bool:
        return self.client is not None

    def get(self, key: str) -> tuple[PredictionCore | None, str]:
        """(core, "hit") | (None, "miss") | (None, "bypass")."""
        if self.client is None:
            self.metrics.cache_bypassed.inc()
            return None, "bypass"
        if self._clock() < self._skip_until:
            self.metrics.cache_bypassed.inc()
            return None, "bypass"
        try:
            raw = self.client.get(key)
        except FAIL_OPEN as exc:
            self._failed("cache_get", exc)
            self.metrics.cache_bypassed.inc()
            return None, "bypass"
        if raw is None:
            self.metrics.cache_misses.inc()
            return None, "miss"
        try:
            core = PredictionCore.model_validate_json(raw)
        except ValueError:  # a value an older or newer code version wrote, or garbage
            self.metrics.redis_errors.labels("cache_decode").inc()
            log.warning("cache entry could not be decoded; recomputing", extra={"key": key})
            self.metrics.cache_misses.inc()
            return None, "miss"
        self.metrics.cache_hits.inc()
        return core, "hit"

    def set(self, key: str, core: PredictionCore) -> None:
        if self.client is None or self._clock() < self._skip_until:
            return
        try:
            self.client.set(key, core.model_dump_json(), ex=self.ttl_s)
        except FAIL_OPEN as exc:
            self._failed("cache_set", exc)

    def _failed(self, op: str, exc: Exception) -> None:
        now = self._clock()
        self.metrics.redis_errors.labels(op).inc()
        self._skip_until = now + self._skip_s
        if now - self._last_logged >= self._log_every_s:  # one line per window, not per request
            self._last_logged = now
            log.warning(
                "redis unavailable; serving uncached",
                extra={"op": op, "error": type(exc).__name__, "skipping_for_s": self._skip_s},
            )
