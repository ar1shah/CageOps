"""Environment-based configuration.

Settings are built lazily via get_settings(). Nothing in this module reads the
environment at import time, so importing a service never fails just because
env vars (or a .env file) are missing, e.g. in CI.
"""

from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from cageops_common.inference.asof import DEFAULT_TIMEZONE


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    redis_url: str
    log_level: str = "INFO"
    # What "today" means for features and predictions (D-037). One variable, read by the feature
    # builder (`rebuild --today` default) and by the API, so a build and a request agree.
    app_timezone: str = DEFAULT_TIMEZONE

    @field_validator("app_timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown time zone {value!r} (use an IANA name)") from exc
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # fields come from the environment
