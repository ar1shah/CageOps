"""Small building blocks shared by every parser: reading cells, and checking a page's shape.

Everything here is pure: text in, value out. Two rules run through all of it:
- A cell the site left blank ("--", "---", empty) becomes None, never 0. A real zero ("0 of 0",
  "0") stays 0.
- A value that is neither blank nor in a format we know raises ParseError. We would rather
  stop and show the odd value than store a guess.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import date, datetime
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

from cageops_scraper.errors import ParseError
from cageops_scraper.sources.ufcstats import UfcStatsSource

CM_PER_INCH = 2.54
_BLANK = re.compile(r"^-*$")  # "", "-", "--", "---": the site's way of saying "no value"
_OF = re.compile(r"^(\d+)\s+of\s+(\d+)$")
_CLOCK = re.compile(r"^(\d+):(\d{2})$")
_HEIGHT = re.compile(r"^(\d+)'\s*(\d+)\"$")
_INCHES = re.compile(r"^(\d+(?:\.\d+)?)\"$")
_ID = re.compile(r"^[0-9a-f]{16}$")
_ROUNDS = re.compile(r"^(\d+)\s*Rnd\b", re.IGNORECASE)

_SOURCE = UfcStatsSource()


# -- text and numbers --------------------------------------------------------------------


def squash(text: str | None) -> str:
    """Collapse all whitespace runs (including newlines and non-breaking spaces) to one space."""
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def is_blank(text: str | None) -> bool:
    return _BLANK.match(squash(text)) is not None


def optional_text(text: str | None) -> str | None:
    """The cleaned text, or None if the site left it blank."""
    cleaned = squash(text)
    return None if is_blank(cleaned) else cleaned


def parse_int(text: str | None) -> int | None:
    cleaned = squash(text)
    if is_blank(cleaned):
        return None
    if not cleaned.isdigit():
        raise ParseError(f"expected a whole number, found {cleaned!r}")
    return int(cleaned)


def parse_of(text: str | None) -> tuple[int | None, int | None]:
    """'40 of 92' -> (40, 92). '0 of 0' -> (0, 0), a real zero. Blank -> (None, None)."""
    cleaned = squash(text)
    if is_blank(cleaned) or cleaned.replace("-", "").replace("of", "").strip() == "":
        return None, None  # "---" and "-- of --" both mean "no value"
    match = _OF.match(cleaned)
    if not match:
        raise ParseError(f"expected 'N of M', found {cleaned!r}")
    return int(match.group(1)), int(match.group(2))


def parse_clock(text: str | None) -> int | None:
    """'0:14' -> 14 seconds, '10:27' -> 627. Blank -> None."""
    cleaned = squash(text)
    if is_blank(cleaned):
        return None
    match = _CLOCK.match(cleaned)
    if not match:
        raise ParseError(f"expected a time like '4:35', found {cleaned!r}")
    return int(match.group(1)) * 60 + int(match.group(2))


def parse_height_cm(text: str | None) -> float | None:
    """'5\\' 10"' -> 177.8. Blank -> None."""
    cleaned = squash(text)
    if is_blank(cleaned):
        return None
    match = _HEIGHT.match(cleaned)
    if not match:
        raise ParseError(f"expected a height like 5' 10\", found {cleaned!r}")
    return round((int(match.group(1)) * 12 + int(match.group(2))) * CM_PER_INCH, 2)


def parse_reach_cm(text: str | None) -> float | None:
    """'71"' -> 180.34. Blank -> None."""
    cleaned = squash(text)
    if is_blank(cleaned):
        return None
    match = _INCHES.match(cleaned)
    if not match:
        raise ParseError(f'expected a reach like 71", found {cleaned!r}')
    return round(float(match.group(1)) * CM_PER_INCH, 2)


def parse_date(text: str | None) -> date | None:
    """'October 03, 2026', 'Jul 20, 1986' and 'Apr. 18, 2026' all work. Blank -> None."""
    cleaned = squash(text)
    if is_blank(cleaned):
        return None
    normalized = cleaned.replace(".", "").replace("Sept ", "Sep ")
    for pattern in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(normalized, pattern).date()
        except ValueError:
            continue
    raise ParseError(f"expected a date like 'April 18, 2026', found {cleaned!r}")


def parse_scheduled_rounds(time_format: str | None) -> int | None:
    """'3 Rnd (5-5-5)' -> 3, '1 Rnd + OT (12-3)' -> 1. 'No Time Limit' and blank -> None."""
    match = _ROUNDS.match(squash(time_format))
    return int(match.group(1)) if match else None


def ufcstats_id(url: str) -> str:
    """The 16-character id at the end of a ufcstats URL."""
    last = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
    if not _ID.match(last):
        raise ParseError(f"no ufcstats id at the end of {url!r}")
    return last


# -- reading the page --------------------------------------------------------------------


@contextmanager
def parse_context(url: str) -> Iterator[None]:
    """Attach the page URL to any ParseError raised inside, so the DLQ entry says which page."""
    try:
        yield
    except ParseError as exc:
        if exc.url is None:
            raise ParseError(exc.message, url) from exc
        raise


def load_soup(html: str) -> BeautifulSoup:
    """Parse a page, refusing the bot-challenge page and anything empty."""
    if not html or not html.strip():
        raise ParseError("empty page")
    if _SOURCE.block_reason(200, html) is not None:
        raise ParseError("this is the bot-challenge page, not content")
    return BeautifulSoup(html, "lxml")


def must_find(parent: Tag | BeautifulSoup, what: str, *args, **kwargs) -> Tag:
    """parent.find(...) that raises ParseError ("missing: <what>") instead of returning None."""
    found = parent.find(*args, **kwargs)
    if not isinstance(found, Tag):
        raise ParseError(f"layout changed: missing {what}")
    return found


def cell_text(tag: Tag) -> str:
    return squash(tag.get_text(" "))


def paragraphs(cell: Tag) -> list[str]:
    """The text of each <p> in a cell. ufcstats puts one value per fighter in its own <p>."""
    return [cell_text(p) for p in cell.find_all("p")]


def _normalize_header(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def check_table(table: Tag, what: str, columns: int, key_headers: Mapping[int, str]) -> None:
    """Check a table's shape: its column count and a few key headers.

    Deliberately loose, so a cosmetic fix on the site's side doesn't break every page:
    headers are compared lowercased with punctuation removed, only the few listed positions are
    checked, and each is a "contains" test ("sigstr" matches both "Sig. str" and "Sig. str.").
    Cells are always read by position, never by header text. Negative positions count from the
    end (-1 is the last column).
    """
    head = table.find("thead")
    headers = [_normalize_header(cell_text(th)) for th in head.find_all("th")] if head else []
    if len(headers) != columns:
        raise ParseError(f"layout changed: {what} has {len(headers)} columns, expected {columns}")
    for position, wanted in key_headers.items():
        if _normalize_header(wanted) not in headers[position]:
            raise ParseError(
                f"layout changed: {what} column {position} is {headers[position]!r}, "
                f"expected it to contain {wanted!r}"
            )
