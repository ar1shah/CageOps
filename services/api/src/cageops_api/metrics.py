"""Prometheus metrics, on a registry of our own.

One `ApiMetrics` per app instead of module-level collectors on the global registry: a test can
build an app, read its numbers and throw it away, and two apps in one process never share (or
double-register) a series.
"""

from __future__ import annotations

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    PlatformCollector,
    ProcessCollector,
)

# 1 ms to 5 s. A warm cached prediction should land in the first few buckets and a cold one in
# the 10-100 ms range, so the resolution is concentrated there (percentiles are interpolated
# inside a bucket, so a coarse bucket is a coarse percentile).
LATENCY_BUCKETS = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)

UNMATCHED = "unmatched"  # the route label for a path no route matched (404s and scanners)


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
