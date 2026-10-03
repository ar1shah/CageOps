"""Parse an event page (/event-details/<id>): the card, with results if it has happened."""

from __future__ import annotations

from bs4 import Tag

from cageops_scraper.errors import ParseError
from cageops_scraper.parsers.common import (
    cell_text,
    check_table,
    load_soup,
    must_find,
    optional_text,
    paragraphs,
    parse_clock,
    parse_context,
    parse_date,
    parse_int,
    squash,
    ufcstats_id,
)
from cageops_scraper.parsers.models import EventBout, EventPage, FighterRef

# The flag in the first column of a finished bout. Anything else is a ParseError, not a guess.
_OUTCOMES = {"win": "win", "draw": "draw", "nc": "no_contest"}
TABLE_COLUMNS = 10  # W/L, Fighter, Kd, Str, Td, Sub, Weight class, Method, Round, Time


def split_location(raw: str | None) -> tuple[str | None, str | None, str | None]:
    """'Las Vegas, Nevada, USA' -> (city, state, country). Two parts mean no state
    ('Tokyo, Japan'); one part is only a city; more than three put the middle in state."""
    if raw is None:
        return None, None, None
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    if len(parts) == 1:
        return parts[0], None, None
    if len(parts) == 2:
        return parts[0], None, parts[1]
    return parts[0], ", ".join(parts[1:-1]), parts[-1]


def parse_event(html: str, url: str) -> EventPage:
    with parse_context(url):
        soup = load_soup(html)
        title = must_find(soup, "the event title", "span", class_="b-content__title-highlight")
        info = _parse_info(soup)
        event_date = parse_date(info.get("date"))
        if event_date is None:
            raise ParseError("the event page has no date")
        location = optional_text(info.get("location"))
        city, state, country = split_location(location)

        table = must_find(soup, "the fights table", "table", class_="b-fight-details__table")
        check_table(
            table,
            "event fights table",
            columns=TABLE_COLUMNS,
            key_headers={1: "fighter", 6: "weight", 7: "method"},
        )
        body = must_find(table, "the fights table body", "tbody")
        bouts = [_parse_bout(row) for row in body.find_all("tr", attrs={"data-link": True})]
        return EventPage(
            ufcstats_id=ufcstats_id(url),
            name=cell_text(title),
            event_date=event_date,
            location_raw=location,
            city=city,
            state=state,
            country=country,
            bouts=bouts,
        )


def _parse_info(soup) -> dict[str, str]:
    """The 'Date:' and 'Location:' list, as {'date': ..., 'location': ...}."""
    box = must_find(soup, "the event info list", "ul", class_="b-list__box-list")
    info = {}
    for item in box.find_all("li"):
        label = item.find("i", class_="b-list__box-item-title")
        if label is None:
            continue
        key = squash(label.get_text()).rstrip(":").lower()
        info[key] = squash(item.get_text(" ").replace(label.get_text(), "", 1))
    return info


def _parse_bout(row: Tag) -> EventBout:
    fight_url = str(row["data-link"])
    cells = row.find_all("td")
    if len(cells) != TABLE_COLUMNS:
        raise ParseError(f"layout changed: a bout row has {len(cells)} cells, expected 10")

    people = cells[1].find_all("a", href=True)
    if len(people) != 2:
        raise ParseError(f"expected 2 fighters in a bout row, found {len(people)}")
    first, second = (
        FighterRef(ufcstats_id=ufcstats_id(str(a["href"])), name=cell_text(a)) for a in people
    )

    flag = cells[0].find(class_="b-flag__text")
    outcome = None
    if flag is not None and (flag_text := squash(flag.get_text()).lower()):
        if flag_text not in _OUTCOMES:
            raise ParseError(f"unknown result flag {flag_text!r}")
        outcome = _OUTCOMES[flag_text]

    method = paragraphs(cells[7])
    return EventBout(
        fight_id=ufcstats_id(fight_url),
        fight_url=fight_url,
        fighters=(first, second),
        outcome=outcome,
        winner_id=first.ufcstats_id if outcome == "win" else None,
        weight_class_raw=optional_text(cell_text(cells[6])),
        method_raw=optional_text(method[0]) if method else None,
        method_detail_raw=optional_text(method[1]) if len(method) > 1 else None,
        round=parse_int(cell_text(cells[8])),
        time_sec=parse_clock(cell_text(cells[9])),
    )
