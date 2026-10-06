"""Shared test fixtures: a real Postgres test database built from the Alembic migrations.

Integration tests use real Postgres (not SQLite) because we rely on ON CONFLICT, CHECK
constraints and CREATE EXTENSION. Locally the tests skip with a clear reason if Postgres
isn't reachable. In CI set REQUIRE_DB=1 so an unreachable database fails instead of skipping.
The same goes for Redis (the rate limiter and circuit breaker are tested against real Redis,
because their correctness depends on Redis running a script atomically): REQUIRE_REDIS=1.
"""

import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import Engine, make_url, text
from sqlalchemy.exc import OperationalError

from cageops_common.config import get_settings
from cageops_common.db.session import make_engine

ALEMBIC_INI = Path(__file__).parent / "packages" / "cageops_common" / "alembic.ini"
TEST_DB_NAME = "cageops_test"


def _unavailable(reason: str):
    if os.environ.get("REQUIRE_DB") == "1":
        pytest.fail(f"REQUIRE_DB=1 but the test database is unavailable: {reason}")
    pytest.skip(reason)


@pytest.fixture(scope="session")
def engine() -> Engine:
    try:
        base_url = os.environ.get("DATABASE_URL") or get_settings().database_url
    except ValidationError:
        _unavailable("DATABASE_URL is not set (copy .env.example to .env)")

    url = make_url(base_url)
    admin = make_engine(base_url).execution_options(isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": TEST_DB_NAME}
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))
    except OperationalError as exc:
        _unavailable(f"cannot reach Postgres: {exc.orig}")
    finally:
        admin.dispose()

    test_engine = make_engine(url.set(database=TEST_DB_NAME).render_as_string(hide_password=False))
    # Always rebuild from migrations so the tests exercise the real schema.
    with test_engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
        cfg = Config(str(ALEMBIC_INI))
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
    yield test_engine
    test_engine.dispose()


TEST_REDIS_DB = 15  # dev data lives in db 0; tests only ever touch (and wipe) db 15


@pytest.fixture
def redis_client():
    """A Redis client on a scratch database that is emptied before every test.

    Skips if Redis isn't reachable; with REQUIRE_REDIS=1 (CI) that is a failure instead.
    Decoded responses are off on purpose: RQ needs bytes, so our code must work with bytes.
    """
    import redis
    from redis.exceptions import ConnectionError as RedisConnectionError

    try:
        base_url = os.environ.get("REDIS_URL") or get_settings().redis_url
    except ValidationError:
        base_url = "redis://localhost:6379/0"
    url = urlsplit(base_url)._replace(path=f"/{TEST_REDIS_DB}").geturl()
    client = redis.Redis.from_url(url)
    try:
        client.flushdb()
    except RedisConnectionError as exc:
        client.close()
        reason = f"cannot reach Redis: {exc}"
        if os.environ.get("REQUIRE_REDIS") == "1":
            pytest.fail(f"REQUIRE_REDIS=1 but Redis is unavailable: {reason}")
        pytest.skip(reason)
    yield client
    client.flushdb()
    client.close()


@pytest.fixture
def alembic_config() -> Config:
    return Config(str(ALEMBIC_INI))


@pytest.fixture
def db(engine: Engine) -> Engine:
    """An engine whose tables are empty at the start of each test."""
    with engine.begin() as conn:
        tables = conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        ).scalars()
        names = ", ".join(f'"{t}"' for t in tables if t != "alembic_version")
        conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    return engine
