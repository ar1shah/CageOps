"""Everything the fetcher can raise, grouped by what the caller should do about it.

- RetryableFetchError: try again later (timeouts, 5xx, 429). Jobs retry with backoff.
- PermanentFetchError / NotFound: asking again gives the same answer. Don't retry.
- SourceBlocked: the site is refusing automated clients. Stop everything, a human decides.
- RobotsDisallowed / RobotsUnavailable: we may not (or can't tell if we may) crawl. Refuse to run.
"""


class FetchError(Exception):
    pass


class RetryableFetchError(FetchError):
    pass


class PermanentFetchError(FetchError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class NotFound(PermanentFetchError):
    def __init__(self, url: str):
        super().__init__(f"404 for {url}", status=404)


class SourceBlocked(FetchError):
    """The circuit breaker is open: the site served a bot challenge or refused us."""

    def __init__(self, reason: str, url: str | None = None):
        super().__init__(f"source blocked ({reason})" + (f" at {url}" if url else ""))
        self.reason = reason
        self.url = url


class RobotsDisallowed(FetchError):
    def __init__(self, path: str):
        super().__init__(f"robots.txt disallows {path}")
        self.path = path


class RobotsUnavailable(FetchError):
    pass
