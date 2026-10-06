"""Parse a "{year} in UFC" page: the past-events table, with each event's article."""

from __future__ import annotations

import re
from datetime import datetime

from bs4 import Tag

from cageops_scraper.errors import ParseError
from cageops_scraper.parsers.common import cell_text, must_find, parse_context
from cageops_scraper.parsers.wikipedia_common import load_wikipedia_soup, title_from_href
from cageops_scraper.parsers.wikipedia_models import WikiEventRef

_NUMBER = re.compile(r"^\d+$")


def parse_year_list(html: str, url: str) -> list[WikiEventRef]:
    """Every event in the "Past events" table, in page order (newest first). Event rows start with
    an event number; the bonus-winner rows under a numbered event are skipped."""
    with parse_context(url):
        soup = load_wikipedia_soup(html)
        heading = must_find(soup, "the Past events heading", id="Past_events")
        table = heading.find_next("table", class_="wikitable")
        if not isinstance(table, Tag):
            raise ParseError("layout changed: missing the past-events table")
        _check_header(table)
        return [_parse_row(row) for row in table.find_all("tr") if _is_event_row(row)]


def _check_header(table: Tag) -> None:
    first = table.find("tr")
    headers = [cell_text(c).lower() for c in first.find_all(["th", "td"])] if first else []
    ok = len(headers) >= 3 and headers[0] == "#" and "event" in headers[1] and "date" in headers[2]
    if not ok:
        raise ParseError(f"layout changed: the past-events table starts with {headers[:3]!r}")


def _is_event_row(row: Tag) -> bool:
    cells = row.find_all(["th", "td"])
    is_numbered = len(cells) >= 3 and cells[0].name == "td"
    return is_numbered and _NUMBER.match(cell_text(cells[0])) is not None


def _parse_row(row: Tag) -> WikiEventRef:
    cells = row.find_all(["th", "td"])
    name = cell_text(cells[1])
    try:
        event_date = datetime.strptime(cell_text(cells[2]), "%b %d, %Y").date()
    except ValueError as exc:
        raise ParseError(f"event {name!r}: expected a date like 'Oct 3, 2026'") from exc
    title = next(
        (t for a in cells[1].find_all("a", href=True) if (t := title_from_href(str(a["href"])))),
        None,
    )
    return WikiEventRef(title=title, name=name, event_date=event_date)
