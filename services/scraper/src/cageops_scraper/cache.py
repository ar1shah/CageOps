"""Raw HTML cache in Postgres (the raw_pages table). One row per URL, the latest copy.

This class only stores and loads. Whether a stored copy is still fresh is decided by the
Source's cache policy (D-015), so the rules for each kind of page live in one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Engine, select
from sqlalchemy.dialects.postgresql import insert

from cageops_common.db.models import RawPage


@dataclass(frozen=True)
class CachedPage:
    url: str
    status: int
    html: str
    fetched_at: datetime


class PageCache:
    def __init__(self, engine: Engine, source: str):
        self._engine = engine
        self._source = source

    def get(self, url: str) -> CachedPage | None:
        """The stored copy, or None. A row stored by a different source is never returned."""
        stmt = select(RawPage.url, RawPage.status, RawPage.html, RawPage.fetched_at).where(
            RawPage.url == url, RawPage.source == self._source
        )
        with self._engine.connect() as conn:
            row = conn.execute(stmt).one_or_none()
        return CachedPage(*row) if row else None

    def put(self, url: str, status: int, html: str, fetched_at: datetime) -> None:
        """Store (or replace) the copy for this URL."""
        values = {
            "url": url,
            "source": self._source,
            "status": status,
            "html": html,
            "fetched_at": fetched_at,
        }
        stmt = insert(RawPage).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["url"], set_={k: v for k, v in values.items() if k != "url"}
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)
