"""Prometheus metrics, on a registry of our own.

One `ApiMetrics` per app instead of module-level collectors on the global registry: a test can
build an app, read its numbers and throw it away, and two apps in one process never share (or
double-register) a series.
"""

from __future__ import annotations

from prometheus_client import (
    CollectorRegistry,
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
