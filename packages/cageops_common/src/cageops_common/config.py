"""Environment-based configuration.

Settings are built lazily via get_settings(). Nothing in this module reads the
environment at import time, so importing a service never fails just because
env vars (or a .env file) are missing, e.g. in CI.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    redis_url: str
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # fields come from the environment
