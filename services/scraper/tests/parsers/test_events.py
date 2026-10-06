import re
from datetime import date

import pytest

from cageops_scraper.errors import ParseError
from cageops_scraper.parsers.events import parse_events_list

COMPLETED = "http://ufcstats.com/statistics/events/completed"
UPCOMING = "http://ufcstats.com/statistics/events/upcoming"


def test_completed_list_has_25_events_newest_first(fixture_html):
    rows = parse_events_list(fixture_html("events_completed"), COMPLETED)

    assert len(rows) == 25
    assert len({r.ufcstats_id for r in rows}) == 25
    assert [r.event_date for r in rows] == sorted((r.event_date for r in rows), reverse=True)


def test_the_first_completed_row_is_the_next_upcoming_event_and_is_flagged(fixture_html):
    rows = parse_events_list(fixture_html("events_completed"), COMPLETED)

    first, second = rows[0], rows[1]
    assert first.is_next_marker
    assert (first.ufcstats_id, first.name, first.event_date) == (
        "7f98d9d5a10fa25c",
        "UFC Fight Night: Allen vs. Duncan",
        date(2026, 10, 10),  # in the future, although it is on the "completed" list
    )
    assert first.location_raw == "Las Vegas, Nevada, USA"
    assert first.url == "http://ufcstats.com/event-details/7f98d9d5a10fa25c"
    # an actual completed event: the next row, with no marker
    assert (second.ufcstats_id, second.name, second.event_date) == (
        "ad3fdba28a7540cf",
        "UFC 332: Silva vs. Wang",
        date(2026, 10, 3),
    )
    assert not second.is_next_marker
    assert sum(r.is_next_marker for r in rows) == 1


def test_upcoming_list_is_soonest_first_with_no_marker(fixture_html):
    rows = parse_events_list(fixture_html("events_upcoming"), UPCOMING)

    assert len(rows) == 8
    assert [r.event_date for r in rows] == sorted(r.event_date for r in rows)
    assert not any(r.is_next_marker for r in rows)
    assert rows[0].ufcstats_id == "7f98d9d5a10fa25c"  # the same event the completed list flags
    assert rows[2].name == "UFC 333: Volkanovski vs. Evloev"
    assert rows[2].location_raw == "Abu Dhabi, Abu Dhabi, United Arab Emirates"


def test_an_upcoming_list_with_no_events_is_an_empty_list_not_an_error(fixture_html):
    html = fixture_html("events_upcoming")
    start, end = html.index("<tbody>"), html.index("</tbody>")

    assert parse_events_list(html[:start] + "<tbody></tbody>" + html[end + 8 :], UPCOMING) == []


def test_a_row_with_no_location_keeps_none_not_an_empty_string(fixture_html):
    html = fixture_html("events_upcoming").replace("Las Vegas, Nevada, USA", "--", 1)

    assert parse_events_list(html, UPCOMING)[0].location_raw is None


@pytest.mark.parametrize("fixture", ["fight_32054bf2b36b0e47", "fighter_23024fdfc966410a"])
def test_other_page_types_are_rejected(fixture_html, fixture):
    with pytest.raises(ParseError, match="events table"):
        parse_events_list(fixture_html(fixture), COMPLETED)


def test_the_challenge_page_and_empty_input_raise_with_the_url(fixture_html):
    with pytest.raises(ParseError, match="bot-challenge") as excinfo:
        parse_events_list(fixture_html("challenge_2026-10-03"), COMPLETED)
    assert excinfo.value.url == COMPLETED
    with pytest.raises(ParseError, match="empty"):
        parse_events_list("", COMPLETED)


def test_a_removed_column_is_a_layout_error(fixture_html):
    html = re.sub(r"<th[^>]*>\s*Location\s*</th>", "", fixture_html("events_completed"))

    with pytest.raises(ParseError, match="1 columns, expected 2"):
        parse_events_list(html, COMPLETED)


def test_a_cosmetically_changed_header_still_parses(fixture_html):
    html = fixture_html("events_completed").replace("Name/date", "NAME / DATE")

    assert len(parse_events_list(html, COMPLETED)) == 25


def test_a_row_without_a_date_is_an_error(fixture_html):
    html = fixture_html("events_upcoming").replace("October 10, 2026", "--", 1)

    with pytest.raises(ParseError, match="no date"):
        parse_events_list(html, UPCOMING)
