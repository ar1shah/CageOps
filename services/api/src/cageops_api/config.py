"""API settings, read from environment variables (see .env.example).

Kept apart from `cageops_common.config.Settings` (database and Redis URLs, log level, time zone)
so a worker or the trainer never needs an API variable, and read lazily: importing the app must
not fail because a variable is missing.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
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
    # The database connection pool. These are the numbers SQLAlchemy would use anyway; real ones
    # come from the 2b-2 load test, which is why `api_db_pool_*` is exposed (D-041).
    db_pool_size: int = Field(default=5, ge=1)
    db_max_overflow: int = Field(default=5, ge=0)
    # Seconds a request waits for a free connection before it is answered 503.
    db_pool_timeout_s: float = Field(default=30.0, gt=0)


@lru_cache
def get_api_settings() -> ApiSettings:
    return ApiSettings()
