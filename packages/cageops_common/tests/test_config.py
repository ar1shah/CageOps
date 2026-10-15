import pytest

from cageops_common.config import get_settings


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_get_settings_reads_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    settings = get_settings()

    assert settings.database_url == "postgresql://u:p@localhost/db"
    assert settings.redis_url == "redis://localhost:6379/0"
    assert settings.log_level == "DEBUG"


def test_the_timezone_defaults_to_new_york_and_unknown_zones_are_refused(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("REDIS_URL", "redis://x")
    monkeypatch.delenv("APP_TIMEZONE", raising=False)
    assert get_settings().app_timezone == "America/New_York"

    get_settings.cache_clear()
    monkeypatch.setenv("APP_TIMEZONE", "Mars/Olympus_Mons")
    with pytest.raises(ValueError, match="unknown time zone"):
        get_settings()
