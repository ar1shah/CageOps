import pytest

from cageops_common.config import get_settings
from cageops_scraper.config import get_scraper_settings
from cageops_worker.ingest import context as context_module
from cageops_worker.ingest.config import UnsupportedPlatform, get_ingest_settings
from cageops_worker.ingest.context import build_context, get_context, set_context

UA = "CageOps/0.1 (+https://github.com/ar1shah/cageops; test@cageops.dev)"


@pytest.fixture(autouse=True)
def fresh_settings():
    caches = (get_settings, get_scraper_settings, get_ingest_settings)
    for cache in caches:
        cache.cache_clear()
    yield
    set_context(None)
    for cache in caches:
        cache.cache_clear()


def test_the_real_wiring_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/x")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/15")
    monkeypatch.setenv("SCRAPER_USER_AGENT", UA)
    monkeypatch.setenv("INGEST_QUEUE", "ingest-test")

    ctx = build_context()

    assert ctx.source.name == "ufcstats" and ctx.queue.name == "ingest-test"
    assert ctx.fetcher.user_agent == UA
    assert ctx.redis.get_connection_kwargs()["db"] == 15
    assert not ctx.redis.get_connection_kwargs().get("decode_responses")  # RQ needs bytes


def test_get_context_builds_once_and_set_context_replaces_it(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/x")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/15")
    monkeypatch.setenv("SCRAPER_USER_AGENT", UA)

    first = get_context()

    assert get_context() is first
    set_context(None)
    assert get_context() is not first


def test_the_worker_refuses_to_wire_itself_on_windows(monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")

    with pytest.raises(UnsupportedPlatform, match="WSL2 or Docker"):
        context_module.build_context()
