"""Parse the events list pages (/statistics/events/completed and /upcoming)."""

from __future__ import annotations

from bs4 import Tag

from cageops_scraper.errors import ParseError
from cageops_scraper.parsers.common import (
    cell_text,
    check_table,
    load_soup,
    must_find,
    optional_text,
    parse_context,
    parse_date,
    ufcstats_id,
)
from cageops_scraper.parsers.models import EventListRow


def parse_events_list(html: str, url: str) -> list[EventListRow]:
    """Every event in the list, in page order (newest first for completed, soonest first for
    upcoming). An empty list is fine: there may be no scheduled events."""
    with parse_context(url):
        soup = load_soup(html)
        table = must_find(soup, "the events table", "table", class_="b-statistics__table-events")
        check_table(table, "events table", columns=2, key_headers={0: "name", 1: "location"})
        body = must_find(table, "the events table body", "tbody")
        return [_parse_row(row) for row in body.find_all("tr") if row.find("a")]


def _parse_row(row: Tag) -> EventListRow:
    link = must_find(row, "an event link", "a", href=True)
    cells = row.find_all("td")
    if len(cells) < 2:
        raise ParseError("layout changed: an event row has fewer than 2 cells")
    date_tag = must_find(row, "an event date", "span", class_="b-statistics__date")
    event_date = parse_date(cell_text(date_tag))
    if event_date is None:
        raise ParseError(f"event {cell_text(link)!r} has no date")
    href = str(link["href"])
    return EventListRow(
        ufcstats_id=ufcstats_id(href),
        url=href,
        name=cell_text(link),
        event_date=event_date,
        location_raw=optional_text(cell_text(cells[1])),
        is_next_marker=row.find("img", class_="b-statistics__icon") is not None,
    )
