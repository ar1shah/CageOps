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
