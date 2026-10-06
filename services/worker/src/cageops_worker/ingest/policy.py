"""Which failures are worth retrying, and what to call each one in the dead-letter queue.

Retry what might go away on its own (a timeout, a 503, a busy database). Don't retry what asking
again can't change (a 404, a page we can't parse, a bot challenge): those go to the DLQ at once so
a person looks at them. Anything we don't recognise is a bug, so it is not retried either: it
should reach the DLQ with its traceback quickly, not be repeated five times.
"""

from __future__ import annotations

from dataclasses import dataclass

from redis.exceptions import RedisError
from rq.timeouts import JobTimeoutException
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

from cageops_scraper.errors import (
    NotFound,
    ParseError,
    PermanentFetchError,
    RetryableFetchError,
    RobotsDisallowed,
    RobotsUnavailable,
    SourceBlocked,
)
from cageops_worker.ingest.errors import MappingError, MissingPrerequisite
from cageops_worker.seed.normalize import UnknownValueError


@dataclass(frozen=True)
class Disposition:
    retry: bool
    reason: str  # short and stable: it is what `dlq list` groups and filters by


# Checked in order, so a more specific class must come before its parent (NotFound is a
# PermanentFetchError).
_RULES: tuple[tuple[type[BaseException], Disposition], ...] = (
    (SourceBlocked, Disposition(False, "source_blocked")),
    (NotFound, Disposition(False, "not_found")),
    (PermanentFetchError, Disposition(False, "http_error")),
    (RetryableFetchError, Disposition(True, "fetch_failed")),
    (ParseError, Disposition(False, "parse_error")),
    (MappingError, Disposition(False, "mapping_error")),
    (UnknownValueError, Disposition(False, "mapping_error")),
    (MissingPrerequisite, Disposition(False, "missing_prerequisite")),
    (RobotsDisallowed, Disposition(False, "robots")),
    (RobotsUnavailable, Disposition(False, "robots")),
    (JobTimeoutException, Disposition(True, "timeout")),
    # A database that hiccuped or restarted, or Redis dropping a connection: try again later.
    (OperationalError, Disposition(True, "infrastructure")),
    (InterfaceError, Disposition(True, "infrastructure")),
    (RedisError, Disposition(True, "infrastructure")),
    (DBAPIError, Disposition(False, "database_error")),  # e.g. an integrity error: a bug
)
UNEXPECTED = Disposition(False, "unexpected_error")
RETRIES_EXHAUSTED = "retries_exhausted"


def classify(exc: BaseException) -> Disposition:
    for exc_type, disposition in _RULES:
        if isinstance(exc, exc_type):
            return disposition
    return UNEXPECTED
