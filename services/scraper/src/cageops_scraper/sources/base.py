"""The Source interface: everything that is specific to one website.

Think of a travel power adapter. The queue, cache, rate limiter and dead-letter queue are the
wall socket; each Source is a plug shape. Supporting a new site means writing a new Source,
not changing the plumbing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Protocol


class PageKind(StrEnum):
    ROBOTS = "robots"
    EVENTS_COMPLETED = "events_completed"
    EVENTS_UPCOMING = "events_upcoming"
    EVENT = "event"
    FIGHT = "fight"
    FIGHTER = "fighter"


@dataclass(frozen=True)
class CachePolicy:
    """When is a cached copy still good?

    ttl=None means forever. Otherwise a copy is fresh while younger than `ttl`, or for good once
    it was fetched at or after `final_after` (after the page can no longer change).
    """

    ttl: timedelta | None
    final_after: datetime | None = None

    def is_fresh(self, fetched_at: datetime, now: datetime) -> bool:
        if self.ttl is None:
            return True
        if self.final_after is not None and fetched_at >= self.final_after:
            return True
        return now - fetched_at < self.ttl


class Source(Protocol):
    name: str  # also the Redis key suffix and the raw_pages.source value
    base_url: str
    # Paths robots.txt must allow for us to run at all.
    required_paths: tuple[str, ...]

    def canonical_url(self, url: str) -> str:
        """One spelling per page, so the cache never stores a page twice. Raises ValueError
        for a URL that doesn't belong to this source."""
        ...

    def block_reason(self, status: int, html: str) -> str | None:
        """A short reason if this response means "we are being blocked", else None."""
        ...

    def cache_policy(self, kind: PageKind, status: int, event_date: date | None) -> CachePolicy: ...
