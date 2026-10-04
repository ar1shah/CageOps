import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from rq.timeouts import JobTimeoutException
from sqlalchemy.exc import IntegrityError, OperationalError

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
from cageops_worker.ingest.policy import classify
from cageops_worker.seed.normalize import UnknownValueError


@pytest.mark.parametrize(
    ("exc", "retry", "reason"),
    [
        # worth retrying: it may go away on its own
        (RetryableFetchError("503 from http://x"), True, "fetch_failed"),  # timeouts, 5xx, 429
        (JobTimeoutException("slow"), True, "timeout"),
        (
            OperationalError("SELECT 1", {}, Exception("server closed the connection")),
            True,
            "infrastructure",
        ),
        (RedisConnectionError("connection reset"), True, "infrastructure"),
        # not worth retrying: asking again gives the same answer
        (NotFound("http://x/fight-details/a"), False, "not_found"),
        (PermanentFetchError("410 gone", status=410), False, "http_error"),
        (ParseError("layout changed: missing the result block"), False, "parse_error"),
        (MappingError("a win with no winner"), False, "mapping_error"),
        (UnknownValueError("unknown weight class: 'Strawweight'"), False, "mapping_error"),
        (MissingPrerequisite("no event row for this fight"), False, "missing_prerequisite"),
        (RobotsDisallowed("/fight-details/"), False, "robots"),
        (RobotsUnavailable("503 for robots.txt"), False, "robots"),
        # the site is blocking us: never burn retries on it
        (SourceBlocked("browser_challenge", "http://x"), False, "source_blocked"),
        # a bug (or a constraint violation, which is one): reach the DLQ fast with the traceback
        (IntegrityError("INSERT", {}, Exception("violates constraint")), False, "database_error"),
        (AttributeError("'NoneType' object has no attribute 'find'"), False, "unexpected_error"),
        (KeyError("outcome"), False, "unexpected_error"),
    ],
)
def test_classification(exc, retry, reason):
    disposition = classify(exc)

    assert (disposition.retry, disposition.reason) == (retry, reason)


def test_not_found_is_checked_before_its_parent_class():
    # NotFound is a PermanentFetchError: it must get the more specific reason
    assert classify(NotFound("http://x")).reason == "not_found"
    assert classify(PermanentFetchError("418", status=418)).reason == "http_error"
