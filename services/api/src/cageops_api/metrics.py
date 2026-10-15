"""Prometheus metrics, on a registry of our own.

One `ApiMetrics` per app instead of module-level collectors on the global registry: a test can
build an app, read its numbers and throw it away, and two apps in one process never share (or
double-register) a series.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    PlatformCollector,
    ProcessCollector,
)
from prometheus_client.core import GaugeMetricFamily
from sqlalchemy import Engine

# 1 ms to 5 s. A warm cached prediction should land in the first few buckets and a cold one in
# the 10-100 ms range, so the resolution is concentrated there (percentiles are interpolated
# inside a bucket, so a coarse bucket is a coarse percentile).
LATENCY_BUCKETS = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)

UNMATCHED = "unmatched"  # the route label for a path no route matched (404s and scanners)

# Waiting for a free pooled connection is usually microseconds; when the pool is the bottleneck it
# is the whole story, so the buckets reach well past the request histogram's.
POOL_WAIT_BUCKETS = (0.0001, 0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 30.0)
STAGE_BUCKETS = (0.0001, 0.00025, 0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25)


class PoolCollector:
    """Reads the connection pool at scrape time, so the numbers are never stale and nothing has
    to update them. `engine` is a callable because the engine is built when the app starts."""

    def __init__(self, engine: Callable[[], Engine | None]) -> None:
        self._engine = engine

    def collect(self) -> Iterable[GaugeMetricFamily]:
        engine = self._engine()
        pool = getattr(engine, "pool", None)
        if pool is None or not hasattr(pool, "checkedout"):
            return
        for name, doc, value in (
            ("api_db_pool_checked_out", "Connections in use right now.", pool.checkedout()),
            ("api_db_pool_checked_in", "Pooled connections sitting idle.", pool.checkedin()),
            ("api_db_pool_size", "Configured steady-state pool size.", pool.size()),
            (
                "api_db_pool_overflow",
                "Connections open beyond pool_size (negative: pool not yet filled).",
                pool.overflow(),
            ),
        ):
            family = GaugeMetricFamily(name, doc)
            family.add_metric([], value)
            yield family


class ApiMetrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        ProcessCollector(registry=self.registry)
        PlatformCollector(registry=self.registry)

        self.request_seconds = Histogram(
            "api_request_duration_seconds",
            "Request latency by method, route TEMPLATE and status code.",
            ["method", "route", "status"],
            buckets=LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.in_flight = Gauge(
            "api_in_flight_requests",
            "Requests being handled right now.",
            registry=self.registry,
        )

        # -- the active model (D-036) -------------------------------------------------------
        self.model_info = Gauge(
            "api_model_info",
            "1 for the model version being served. Cleared and reset on every switch.",
            ["version", "features_output_sha256"],
            registry=self.registry,
        )
        self.model_loaded = Gauge(
            "api_model_loaded", "1 when a verified model is loaded.", registry=self.registry
        )
        self.reloads = Counter(
            "api_model_reload_total",
            "Model loads by outcome. The first load at startup counts too.",
            ["result"],
            registry=self.registry,
        )
        self.model_load_seconds = Gauge(
            "api_model_load_seconds",
            "How long the most recent successful load took, verification included.",
            registry=self.registry,
        )
        self.active_in_db = Gauge(
            "api_model_active_in_db",
            "1 when the registry has an active version, 0 when it has none.",
            registry=self.registry,
        )
        self.last_poll = Gauge(
            "api_model_last_poll_timestamp_seconds",
            "Unix time of the last registry poll that reached the database.",
            registry=self.registry,
        )
        for result in ("success", "failure"):
            self.reloads.labels(result)  # exposed at 0 so a rate() has something to start from

        # -- the database pool (D-041) ------------------------------------------------------
        self.pool_wait = Histogram(
            "api_db_pool_checkout_seconds",
            "Time spent waiting for a pooled database connection.",
            buckets=POOL_WAIT_BUCKETS,
            registry=self.registry,
        )
        self.pool_timeouts = Counter(
            "api_db_pool_checkout_timeouts_total",
            "Requests that gave up waiting for a connection (answered 503).",
            registry=self.registry,
        )

        # -- predictions (D-038, D-039) -----------------------------------------------------
        self.predictions = Counter(
            "api_predictions_total",
            "Predictions served, by endpoint.",
            ["endpoint"],
            registry=self.registry,
        )
        self.predictions_stats_gap = Counter(
            "api_predictions_with_stats_gap_total",
            "Predictions served where at least one fighter's rates miss a fight with no "
            "per-round stats (a nonzero stats_gap_fights).",
            registry=self.registry,
        )
        self.predictions_stale = Counter(
            "api_predictions_features_stale_total",
            "Predictions served where a fighter has a completed fight the features do not "
            "include (features_current is false).",
            registry=self.registry,
        )
        self.stage_seconds = Histogram(
            "api_predict_stage_seconds",
            "Where a prediction spends its time: db, assemble, model, build (the response object) "
            "and serialize (the JSON).",
            ["stage"],
            buckets=STAGE_BUCKETS,
            registry=self.registry,
        )

        # -- the prediction cache (D-042) ---------------------------------------------------
        self.cache_hits = Counter(
            "api_cache_hits_total", "Predictions answered from Redis.", registry=self.registry
        )
        self.cache_misses = Counter(
            "api_cache_misses_total",
            "Predictions Redis was asked for and did not have (they are then computed).",
            registry=self.registry,
        )
        self.cache_bypassed = Counter(
            "api_cache_bypassed_total",
            "Predictions computed without consulting the cache: disabled, no features state yet, "
            "or Redis unavailable.",
            registry=self.registry,
        )
        self.redis_errors = Counter(
            "api_redis_errors_total",
            "Redis calls that failed or timed out. The API failed open each time.",
            ["op"],
            registry=self.registry,
        )

        # -- what the features and the registry looked like at the last poll ----------------
        self.features_build_id = Gauge(
            "api_features_build_id",
            "load_runs.id of the latest features build the cache key is using.",
            registry=self.registry,
        )
        self.features_inputs_match = Gauge(
            "api_features_inputs_match_build",
            "1 when what the features were built from is what the database holds now; 0 when a "
            "result, bio or stat changed since the last features rebuild.",
            registry=self.registry,
        )

        # -- rate limiting (D-043) ----------------------------------------------------------
        self.ratelimited = Counter(
            "api_ratelimited_total",
            "Requests refused with 429, by client class (public | frontend) and by which bucket "
            "was empty (ip | user | key).",
            ["client_class", "scope"],
            registry=self.registry,
        )
        self.ratelimit_ignored = Counter(
            "api_ratelimit_ignored_headers_total",
            "Headers that were sent but not believed: a forwarded client IP without a valid API "
            "key, an API key that did not match, or a malformed client IP.",
            ["reason"],
            registry=self.registry,
        )

    def watch_pool(self, engine: Callable[[], Engine | None]) -> None:
        self.registry.register(PoolCollector(engine))
