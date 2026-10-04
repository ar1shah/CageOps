"""Ingestion settings, read from environment variables (see .env.example)."""

from __future__ import annotations

import sys
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

SUPPORTED_PLATFORMS = ("linux", "darwin")


class UnsupportedPlatform(RuntimeError):
    pass


def ensure_supported_platform(platform: str | None = None) -> None:
    """Refuse to start the worker anywhere RQ can't run it (D-022).

    RQ's default job timeout uses SIGALRM, which Windows doesn't have, so on native Windows every
    job would fail the moment it started. Say so up front instead.
    """
    platform = platform or sys.platform
    if platform not in SUPPORTED_PLATFORMS:
        raise UnsupportedPlatform(
            f"the ingestion worker needs Linux or macOS (RQ's timeouts use SIGALRM); this is "
            f"{platform!r}. On Windows, run it inside WSL2 or Docker."
        )


class IngestSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ingest_queue: str = "ingest"
    # Retries after the first attempt, so a job runs at most ingest_max_retries + 1 times.
    ingest_max_retries: int = 5
    # Delay before retry n is a random value in [d/2, d] where d = min(cap, base * 2**n) seconds.
    # 0 means retry immediately (tests use it).
    ingest_retry_base_s: int = 30
    ingest_retry_cap_s: int = 900
    ingest_job_timeout_s: int = 300
    # How long an enqueued job id is "claimed" (D-021). Raise it above the expected run length for
    # a full-history backfill (about 3.3 hours at 1 request/second).
    ingest_claim_ttl_s: int = 7200
    # Per-run row counts (inserted / updated / unchanged) live in Redis this long.
    ingest_run_stats_ttl_s: int = 7 * 24 * 3600

    # The worker's HTTP port: /healthz and /metrics. Containers set the host to 0.0.0.0.
    worker_http_host: str = "127.0.0.1"
    worker_metrics_port: int = 9100  # 0 turns the server off
    # /healthz goes 503 when the worker's loop hasn't ticked for job timeout + this margin (D-024).
    worker_health_margin_s: int = 60
    # While the site is blocking us (circuit breaker open) a worker checks again this often.
    worker_hold_poll_s: float = 30.0
    # How often the background thread re-reads Redis and Postgres for the /healthz body and gauges.
    worker_snapshot_interval_s: float = 10.0
    worker_probe_timeout_s: float = 2.0

    @property
    def worker_max_pulse_age_s(self) -> float:
        return self.ingest_job_timeout_s + self.worker_health_margin_s


@lru_cache
def get_ingest_settings() -> IngestSettings:
    return IngestSettings()
