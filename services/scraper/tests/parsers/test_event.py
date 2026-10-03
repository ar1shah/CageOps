import re
from datetime import date

import pytest

from cageops_scraper.errors import ParseError
from cageops_scraper.parsers.event import parse_event, split_location

BURNS = "http://ufcstats.com/event-details/c3ac8d0da7b05772"
UPCOMING = "http://ufcstats.com/event-details/7f98d9d5a10fa25c"


@pytest.fixture
def completed(fixture_html):
    return parse_event(fixture_html("event_c3ac8d0da7b05772"), BURNS)


@pytest.fixture
def upcoming(fixture_html):
    return parse_event(fixture_html("event_upcoming_7f98d9d5a10fa25c"), UPCOMING)


def by_id(event, fight_id):
    return next(b for b in event.bouts if b.fight_id == fight_id)


# -- the completed card ----------------------------------------------------------------------


def test_event_header_comes_from_the_page(completed):
    assert completed.ufcstats_id == "c3ac8d0da7b05772"
    assert completed.name == "UFC Fight Night: Burns vs. Malott"
    assert completed.event_date == date(2026, 4, 18)  # the only source of the fight date
    assert (completed.city, completed.state, completed.country) == (
        "Winnipeg",
        "Manitoba",
        "Canada",
    )
    assert completed.location_raw == "Winnipeg, Manitoba, Canada"


def test_a_ko_row_lists_the_winner_first_and_has_the_finish(completed):
    bout = by_id(completed, "32054bf2b36b0e47")

    assert [f.name for f in bout.fighters] == ["Mike Malott", "Gilbert Burns"]
    assert bout.outcome == "win"
    assert bout.winner_id == "dd6103dd7127db1d"  # Malott, listed first although billed second
    assert (bout.method_raw, bout.method_detail_raw) == ("KO/TKO", "Punches")
    assert (bout.round, bout.time_sec) == (3, 128)
    assert bout.weight_class_raw == "Welterweight"


def test_decision_and_submission_rows(completed):
    decision = by_id(completed, "b5299e5b946015e5")
    submission = by_id(completed, "9fab4b0ad082f670")

    assert (decision.method_raw, decision.method_detail_raw) == ("U-DEC", None)
    assert [f.name for f in decision.fighters] == ["Charles Jourdain", "Kyler Phillips"]
    assert (decision.round, decision.time_sec) == (3, 300)
    assert (submission.method_raw, submission.method_detail_raw) == ("SUB", "Rear Naked Choke")
    assert (submission.round, submission.time_sec) == (1, 142)


def test_a_draw_has_no_winner(completed):
    bout = by_id(completed, "552f7cdaf93e1055")

    assert bout.outcome == "draw"
    assert bout.winner_id is None
    assert bout.method_raw == "M-DEC"
    assert bout.weight_class_raw == "Catch Weight"
    # a draw has no winner to put first, so this order is the card's own (the red corner first)
    assert [f.name for f in bout.fighters] == ["John Castaneda", "Mark Vologdin"]


def test_the_card_has_twelve_bouts_with_unique_ids(completed):
    assert len(completed.bouts) == 12
    assert len({b.fight_id for b in completed.bouts}) == 12
    assert all(b.outcome is not None for b in completed.bouts)
    assert all(len({f.ufcstats_id for f in b.fighters}) == 2 for b in completed.bouts)


# -- an upcoming card (no results yet) -----------------------------------------------------


def test_an_upcoming_event_has_bouts_but_no_results(upcoming):
    assert upcoming.name == "UFC Fight Night: Allen vs. Duncan"
    assert upcoming.event_date == date(2026, 10, 10)
    assert len(upcoming.bouts) == 12

    first = upcoming.bouts[0]
    assert [f.name for f in first.fighters] == ["Brendan Allen", "Christian Leroy Duncan"]
    assert first.fight_id == "7db1a3dac7e343e7"
    assert first.weight_class_raw == "Middleweight"
    # everything about the result is None (NULL), not 0 and not an empty string
    assert (first.outcome, first.winner_id, first.method_raw, first.method_detail_raw) == (
        None,
        None,
        None,
        None,
    )
    assert (first.round, first.time_sec) == (None, None)
    assert all(b.outcome is None and b.round is None for b in upcoming.bouts)


def test_upcoming_weight_classes_include_womens_divisions(upcoming):
    assert {b.weight_class_raw for b in upcoming.bouts} >= {"Women's Strawweight", "Heavyweight"}


# -- locations -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Las Vegas, Nevada, USA", ("Las Vegas", "Nevada", "USA")),
        (
            "Abu Dhabi, Abu Dhabi, United Arab Emirates",
            ("Abu Dhabi", "Abu Dhabi", "United Arab Emirates"),
        ),
        ("Tokyo, Japan", ("Tokyo", None, "Japan")),
        ("Macau", ("Macau", None, None)),
        ("A, B, C, D", ("A", "B, C", "D")),
        (None, (None, None, None)),
    ],
)
def test_split_location(raw, expected):
    assert split_location(raw) == expected


# -- things that must fail loudly ---------------------------------------------------------------


def test_a_missing_date_is_an_error(fixture_html):
    html = fixture_html("event_c3ac8d0da7b05772").replace("April 18, 2026", "--")

    with pytest.raises(ParseError, match="no date"):
        parse_event(html, BURNS)


def test_an_unknown_result_flag_is_an_error_not_a_guess(fixture_html):
    html = fixture_html("event_c3ac8d0da7b05772").replace(">win<", ">forfeit<", 1)

    with pytest.raises(ParseError, match="forfeit"):
        parse_event(html, BURNS)


def test_a_removed_column_is_a_layout_error(fixture_html):
    html = re.sub(r"<th[^>]*>\s*Time\s*</th>", "", fixture_html("event_c3ac8d0da7b05772"))

    with pytest.raises(ParseError, match="9 columns, expected 10"):
        parse_event(html, BURNS)


def test_a_cosmetic_header_change_still_parses(fixture_html, completed):
    html = fixture_html("event_c3ac8d0da7b05772").replace("Weight class", "WEIGHT CLASS")

    assert parse_event(html, BURNS) == completed


@pytest.mark.parametrize(
    "fixture", ["fight_32054bf2b36b0e47", "events_completed", "fighter_23024fdfc966410a"]
)
def test_other_page_types_are_rejected(fixture_html, fixture):
    with pytest.raises(ParseError):
        parse_event(fixture_html(fixture), BURNS)


def test_the_challenge_page_is_rejected(fixture_html):
    with pytest.raises(ParseError, match="bot-challenge"):
        parse_event(fixture_html("challenge_2026-10-03"), BURNS)


def test_a_url_without_an_id_is_an_error(fixture_html):
    with pytest.raises(ParseError, match="no ufcstats id"):
        parse_event(fixture_html("event_c3ac8d0da7b05772"), "http://ufcstats.com/event-details/")
