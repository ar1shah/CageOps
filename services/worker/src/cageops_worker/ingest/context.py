"""The per-process wiring a job needs: database, Redis, queue and the one fetcher.

Job functions take only plain arguments (RQ stores them in Redis), so they get everything else from
here. A worker builds the context once at start-up (build_context); tests install their own with
set_context. Safe as a module global because the worker runs one job at a time in one process.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime

import httpx
from redis import Redis
from rq import Queue
from sqlalchemy import Engine

from cageops_common.config import get_settings
from cageops_common.db.session import make_engine
from cageops_scraper.config import get_scraper_settings
from cageops_scraper.fetch import Fetcher
from cageops_scraper.sources import Source, get_source
from cageops_worker.ingest.config import (
    IngestSettings,
    ensure_supported_platform,
    get_ingest_settings,
)
from cageops_worker.ingest.queue import get_queue


def _today() -> date:
    return datetime.now(UTC).date()


@dataclass
class IngestContext:
    engine: Engine
    redis: Redis  # NOT decoded: RQ needs bytes
    queue: Queue
    fetcher: Fetcher
    source: Source
    settings: IngestSettings
    today: Callable[[], date] = _today  # tests pin it


_context: IngestContext | None = None


def set_context(context: IngestContext | None) -> None:
    global _context
    _context = context


def get_context() -> IngestContext:
    global _context
    if _context is None:
        _context = build_context()
    return _context


def build_context() -> IngestContext:
    """Wire everything from the environment. Refuses to run on platforms RQ can't support."""
    ensure_supported_platform()
    settings = get_ingest_settings()
    common = get_settings()
    scraper = get_scraper_settings()
    redis = Redis.from_url(common.redis_url)
    engine = make_engine(common.database_url)
    source = get_source(scraper)
    fetcher = Fetcher(source, engine, redis, httpx.Client(), scraper)
    return IngestContext(
        engine=engine,
        redis=redis,
        queue=get_queue(redis, settings),
        fetcher=fetcher,
        source=source,
        settings=settings,
    )
