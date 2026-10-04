"""Errors raised by the ingestion layer itself (fetch and parse errors live in cageops_scraper)."""


class MappingError(ValueError):
    """A parsed page that can't be turned into a database row (an impossible combination of
    result badges, say). Fetching it again gives the same page, so it is never retried."""


class MissingPrerequisite(RuntimeError):
    """A job ran before the rows it depends on exist (a fight job with no event row). The event
    row is where a fight's date comes from, so we refuse to guess one."""
