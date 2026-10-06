"""Prometheus metrics for the scraper. Served over HTTP by /metrics in checkpoint 4.

Defined once at import time on the default registry. Labels are always the source name, so a
replay benchmark and the real site never share a time series.
"""

from prometheus_client import Counter, Histogram

FETCH_SECONDS = Histogram(
    "scraper_fetch_seconds",
    "Time spent on one HTTP request to the source",
    ["source", "status_class"],  # status_class: 2xx, 4xx, 5xx, or "error" (no response)
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30),
)
CACHE_TOTAL = Counter(
    "scraper_cache_total",
    "Raw page cache lookups",
    ["source", "result"],  # hit | miss | stale | forced
)
RATELIMIT_WAIT_SECONDS = Histogram(
    "scraper_ratelimit_wait_seconds",
    "Time a request waited for its rate-limit slot",
    ["source"],
    buckets=(0, 0.1, 0.5, 1, 2, 5, 10, 30, 60, 120),
)
SOURCE_BLOCKED_TOTAL = Counter(
    "scraper_source_blocked_total",
    "Times the source served a bot challenge or refused us (trips the circuit breaker)",
    ["source", "reason"],
)
PARSE_ANOMALY_TOTAL = Counter(
    "scraper_parse_anomaly_total",
    "Suspicious values found while parsing a page that did not stop the parse",
    ["source", "kind"],  # kind: e.g. round_sig_strikes_sum_mismatch, unrecognized_time_format
)
