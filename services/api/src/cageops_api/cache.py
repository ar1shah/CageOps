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
from datetime import date

import redis

from cageops_api.metrics import ApiMetrics
from cageops_api.predict import PredictionCore
from cageops_api.redis_guard import FAIL_OPEN, RedisGuard

log = logging.getLogger(__name__)

CACHE_SCHEMA = "v1"
KEY_PREFIX = f"pred:{CACHE_SCHEMA}:"
SOURCE_ADHOC = "adhoc"  # an ad-hoc matchup, features as of today
SOURCE_BOUT = "bout"  # a scheduled bout, features as of its event date


def slug(value: str | None) -> str:
    """A key segment from a division name. Callers pass a vocabulary entry (the router
    normalizes first); the slug is a second line of defense, leaving only [a-z0-9-], so no
    separator (":") or wildcard can ever come from a caller."""
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
    scheduled_rounds: int | None,
    is_title_fight: bool | None,
    top_n: int,
    as_of: date,
    source: str = SOURCE_ADHOC,
) -> str:
    """`weight_class` None is "auto" for an ad-hoc request (resolved from the fighters' recent
    bouts) and "none" for a scheduled bout whose card names no class. `scheduled_rounds` and
    `is_title_fight` are None for a scheduled bout: ufcstats does not say before the fight (D-044),
    and the key says "unknown" rather than a value."""
    lo, hi = sorted((fighter_a_id, fighter_b_id))
    division = (
        slug(weight_class)
        if weight_class is not None
        else ("none" if source == SOURCE_BOUT else "auto")
    )
    rounds = "x" if scheduled_rounds is None else str(scheduled_rounds)
    title = "x" if is_title_fight is None else str(int(is_title_fight))
    return (
        f"{KEY_PREFIX}{model_version}:{build_id}:{inputs_sha12}:{lo}-{hi}:{division}:"
        f"{rounds}:{title}:top{top_n}:asof={as_of.isoformat()}:src={source}"
    )


class PredictionCache:
    def __init__(
        self,
        client: redis.Redis | None,
        *,
        ttl_s: int,
        metrics: ApiMetrics,
        guard: RedisGuard,
    ) -> None:
        self.client = client
        self.ttl_s = ttl_s
        self.metrics = metrics
        self.guard = guard

    @property
    def enabled(self) -> bool:
        return self.client is not None

    def get(self, key: str) -> tuple[PredictionCore | None, str]:
        """(core, "hit") | (None, "miss") | (None, "bypass")."""
        if self.client is None or not self.guard.available():
            self.metrics.cache_bypassed.inc()
            return None, "bypass"
        try:
            raw = self.client.get(key)
        except FAIL_OPEN as exc:
            self.guard.failed("cache_get", exc)
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

    def get_many(self, keys: list[str]) -> tuple[list[PredictionCore | None], str]:
        """One round trip for every key. The status is "hit" (all), "miss" (none), "partial", or
        "bypass" (Redis unavailable: nothing was asked, every entry is None)."""
        if self.client is None or not keys or not self.guard.available():
            self.metrics.cache_bypassed.inc(len(keys) or 1)
            return [None] * len(keys), "bypass"
        try:
            raws = self.client.mget(keys)
        except FAIL_OPEN as exc:
            self.guard.failed("cache_get", exc)
            self.metrics.cache_bypassed.inc(len(keys))
            return [None] * len(keys), "bypass"
        cores: list[PredictionCore | None] = []
        for key, raw in zip(keys, raws, strict=True):
            core = None
            if raw is not None:
                try:
                    core = PredictionCore.model_validate_json(raw)
                except ValueError:
                    self.metrics.redis_errors.labels("cache_decode").inc()
                    log.warning("cache entry could not be decoded; recomputing", extra={"key": key})
            cores.append(core)
            (self.metrics.cache_hits if core is not None else self.metrics.cache_misses).inc()
        found = sum(c is not None for c in cores)
        status = "hit" if found == len(cores) else "miss" if found == 0 else "partial"
        return cores, status

    def set_many(self, items: list[tuple[str, PredictionCore]]) -> None:
        if self.client is None or not items or not self.guard.available():
            return
        try:
            pipe = self.client.pipeline(transaction=False)
            for key, core in items:
                pipe.set(key, core.model_dump_json(), ex=self.ttl_s)
            pipe.execute()
        except FAIL_OPEN as exc:
            self.guard.failed("cache_set", exc)

    def set(self, key: str, core: PredictionCore) -> None:
        if self.client is None or not self.guard.available():
            return
        try:
            self.client.set(key, core.model_dump_json(), ex=self.ttl_s)
        except FAIL_OPEN as exc:
            self.guard.failed("cache_set", exc)
