"""API settings, read from environment variables (see .env.example).

Kept apart from `cageops_common.config.Settings` (database and Redis URLs, log level, time zone)
so a worker or the trainer never needs an API variable, and read lazily: importing the app must
not fail because a variable is missing.
"""

from __future__ import annotations

import ipaddress
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ApiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Where model bundles live, one directory per version. The same variable the trainer writes to
    # (`MODEL_ARTIFACT_DIR`). The bundle for a version is always `<dir>/<version>`: the registry's
    # stored artifact_path was relative to wherever the trainer ran, so it is not used (D-035).
    model_artifact_dir: Path = Path("data/models")
    # How often to ask the registry which version is active (D-036). Up to this long after
    # `activate`, a replica still serves the old version.
    model_poll_interval_s: float = Field(default=30.0, gt=0)
    # After a version fails verification, wait this long before trying that version again.
    model_reload_retry_s: float = Field(default=300.0, ge=0)
    # How many contributing features /predict returns by default (D-038).
    predict_top_features: int = Field(default=5, ge=1, le=10)
    # The Redis prediction cache (D-042). It never decides whether a prediction is served: when
    # Redis is down or slow the API computes the answer itself and counts the failure.
    cache_enabled: bool = True
    # Safety net, not the invalidation mechanism (the key is): bounds what the key cannot see
    # (a renamed fighter, a result overturned before the next poll) and Redis memory.
    cache_ttl_s: int = Field(default=21600, gt=0)
    # Socket timeout for every Redis call, in milliseconds. Short on purpose: a slow cache must
    # cost less than recomputing.
    redis_timeout_ms: int = Field(default=100, gt=0)
    # Per-client rate limiting (D-043): a token bucket in Redis, 429 + Retry-After. /healthz,
    # /readyz and /metrics are never limited. When Redis is down requests are allowed.
    rate_limit_enabled: bool = True
    rate_limit_per_minute: float = Field(default=120.0, gt=0)  # sustained rate per client IP
    rate_limit_burst: float = Field(default=40.0, ge=1)  # bucket size per client IP
    # Comma-separated CIDRs of proxies we sit behind (Caddy in 2c). X-Forwarded-For is believed
    # only from these. Empty (the default) means no proxy is trusted: anyone can send any
    # X-Forwarded-For, so believing it would let a client pick its own bucket.
    trusted_proxy_cidrs: str = ""
    # Server-to-server key for the Next.js frontend (D-043). At least 32 characters or the
    # frontend class is off. Never logged, never a metric label. FRONTEND_API_KEY_PREVIOUS is for
    # a rotation overlap.
    frontend_api_key: SecretStr = SecretStr("")
    frontend_api_key_previous: SecretStr = SecretStr("")
    frontend_user_rate_per_minute: float = Field(default=300.0, gt=0)
    frontend_user_burst: float = Field(default=100.0, ge=1)
    frontend_key_rate_per_minute: float = Field(default=6000.0, gt=0)
    frontend_key_burst: float = Field(default=600.0, ge=1)
    # The database connection pool. These are the numbers SQLAlchemy would use anyway; real ones
    # come from the 2b-2 load test, which is why `api_db_pool_*` is exposed (D-041).
    db_pool_size: int = Field(default=5, ge=1)
    db_max_overflow: int = Field(default=5, ge=0)
    # Seconds a request waits for a free connection before it is answered 503.
    db_pool_timeout_s: float = Field(default=30.0, gt=0)

    @field_validator("trusted_proxy_cidrs")
    @classmethod
    def _cidrs_parse(cls, value: str) -> str:
        for part in value.split(","):
            if part.strip():
                ipaddress.ip_network(part.strip())  # raises ValueError naming the bad one
        return value

    def trusted_proxies(self) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
        return tuple(
            ipaddress.ip_network(part.strip())
            for part in self.trusted_proxy_cidrs.split(",")
            if part.strip()
        )

    def frontend_keys(self) -> tuple[bytes, ...]:
        """The keys that count: only those long enough to be a secret."""
        keys = (self.frontend_api_key, self.frontend_api_key_previous)
        return tuple(k.get_secret_value().encode() for k in keys if len(k.get_secret_value()) >= 32)


@lru_cache
def get_api_settings() -> ApiSettings:
    return ApiSettings()
