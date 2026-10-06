import pytest
from bs4 import BeautifulSoup

from cageops_scraper.errors import ParseError
from cageops_scraper.parsers.common import (
    check_table,
    load_soup,
    must_find,
    optional_text,
    paragraphs,
    parse_clock,
    parse_context,
    parse_date,
    parse_height_cm,
    parse_int,
    parse_of,
    parse_reach_cm,
    parse_scheduled_rounds,
    squash,
    ufcstats_id,
)

# -- blank is None, a real zero stays zero -----------------------------------------------


@pytest.mark.parametrize("blank", ["", "  ", "-", "--", "---", "\n  --  \n", None])
def test_blank_cells_become_none_not_zero(blank):
    assert parse_int(blank) is None
    assert parse_of(blank) == (None, None)
    assert parse_clock(blank) is None
    assert parse_height_cm(blank) is None
    assert parse_reach_cm(blank) is None
    assert parse_date(blank) is None
    assert optional_text(blank) is None


def test_real_zeros_stay_zero():
    assert parse_int("0") == 0
    assert parse_of("0 of 0") == (0, 0)  # published as 0, so it is 0
    assert parse_clock("0:00") == 0


def test_a_blank_part_of_an_of_cell_is_missing_not_zero():
    assert parse_of("-- of --") == (None, None)


@pytest.mark.parametrize("junk", ["abc", "3.5", "1,000"])
def test_an_unknown_integer_format_raises_instead_of_guessing(junk):
    with pytest.raises(ParseError, match="whole number"):
        parse_int(junk)


# -- formats seen on the real pages -------------------------------------------------------


def test_of_cells():
    assert parse_of("40 of 92") == (40, 92)
    assert parse_of("\n   56 of 121\n") == (56, 121)
    with pytest.raises(ParseError, match="N of M"):
        parse_of("40/92")


def test_clock_cells():
    assert parse_clock("0:14") == 14
    assert parse_clock("2:08") == 128
    assert parse_clock("10:27") == 627  # the 1998 "1 Rnd + OT (12-3)" fight
    with pytest.raises(ParseError, match="time"):
        parse_clock("2m08s")


def test_height_and_reach_convert_to_the_seeds_centimetres():
    assert parse_height_cm("5' 10\"") == 177.8  # Gilbert Burns, same as the seed
    assert parse_reach_cm('71"') == 180.34  # Gilbert Burns, same as the seed
    assert parse_height_cm("6' 0\"") == 182.88
    assert parse_reach_cm('72"') == 182.88
    with pytest.raises(ParseError, match="height"):
        parse_height_cm("178 cm")
    with pytest.raises(ParseError, match="reach"):
        parse_reach_cm("180 cm")


def test_dates_in_every_spelling_the_site_uses():
    from datetime import date

    assert parse_date("October 03, 2026") == date(2026, 10, 3)  # events list
    assert parse_date("April 18, 2026") == date(2026, 4, 18)  # event page
    assert parse_date("Jul 20, 1986") == date(1986, 7, 20)  # fighter DOB
    assert parse_date("Apr. 18, 2026") == date(2026, 4, 18)  # fighter fight history
    assert parse_date("Sept. 5, 2020") == date(2020, 9, 5)
    with pytest.raises(ParseError, match="date"):
        parse_date("18/04/2026")


def test_scheduled_rounds_from_the_time_format():
    assert parse_scheduled_rounds("3 Rnd (5-5-5)") == 3
    assert parse_scheduled_rounds("5 Rnd (5-5-5-5-5)") == 5
    assert parse_scheduled_rounds("1 Rnd + OT (12-3)") == 1
    assert parse_scheduled_rounds("No Time Limit") is None
    assert parse_scheduled_rounds(None) is None


def test_ids_come_from_the_url():
    assert ufcstats_id("http://ufcstats.com/fight-details/32054bf2b36b0e47") == "32054bf2b36b0e47"
    assert ufcstats_id("http://ufcstats.com/fight-details/32054bf2b36b0e47/?x=1") == (
        "32054bf2b36b0e47"
    )
    with pytest.raises(ParseError, match="no ufcstats id"):
        ufcstats_id("http://ufcstats.com/statistics/events/completed")


def test_squash_collapses_whitespace_and_non_breaking_spaces():
    assert squash("  Gilbert \n  Burns\xa0 ") == "Gilbert Burns"


# -- reading a page ------------------------------------------------------------------------


def test_the_bot_challenge_page_and_empty_pages_are_refused(fixture_html):
    with pytest.raises(ParseError, match="bot-challenge"):
        load_soup(fixture_html("challenge_2026-10-03"))
    with pytest.raises(ParseError, match="empty"):
        load_soup("   ")


def test_parse_context_adds_the_url_once():
    with pytest.raises(ParseError) as excinfo, parse_context("http://x/fight-details/abc"):
        raise ParseError("layout changed: missing thing")

    assert excinfo.value.url == "http://x/fight-details/abc"
    assert "missing thing" in str(excinfo.value) and "fight-details/abc" in str(excinfo.value)

    with pytest.raises(ParseError) as inner, parse_context("http://outer"):
        raise ParseError("boom", "http://inner")
    assert inner.value.url == "http://inner"  # an existing url is kept


def test_must_find_raises_a_parse_error_naming_what_is_missing():
    soup = BeautifulSoup("<div><p>hi</p></div>", "lxml")

    assert must_find(soup, "a paragraph", "p").get_text() == "hi"
    with pytest.raises(ParseError, match="missing the result block"):
        must_find(soup, "the result block", "div", class_="b-fight-details__content")


def test_paragraphs_returns_one_string_per_fighter():
    cell = BeautifulSoup("<td><p> 40 of 92 </p><p>\n56 of 121\n</p></td>", "lxml").td

    assert paragraphs(cell) == ["40 of 92", "56 of 121"]


# -- the loose layout check ---------------------------------------------------------------


def _table(headers: list[str]) -> BeautifulSoup:
    ths = "".join(f"<th> {h} </th>" for h in headers)
    return BeautifulSoup(f"<table><thead><tr>{ths}</tr></thead></table>", "lxml").table


TOTALS = [
    "Fighter",
    "KD",
    "Sig. str.",
    "Sig. str. %",
    "Total str.",
    "Td %",
    "Td %",
    "Sub. att",
    "Rev.",
    "Ctrl",
]
KEYS = {0: "fighter", 1: "kd", 2: "sig str", -1: "ctrl"}


def test_the_real_header_with_its_typo_passes():
    check_table(_table(TOTALS), "totals table", columns=10, key_headers=KEYS)


@pytest.mark.parametrize(
    "edit",
    [
        lambda h: [x.replace("Td %", "Td") for x in h],  # the typo gets fixed
        lambda h: [x.replace(".", "") for x in h],  # punctuation changes
        lambda h: [x.upper() for x in h],  # capitalization changes
        lambda h: ["Sig. str" if x == "Sig. str." else x for x in h],  # trailing dot dropped
        lambda h: [x + " " for x in h],  # whitespace
    ],
    ids=["typo-fixed", "no-punctuation", "uppercase", "trailing-dot", "whitespace"],
)
def test_cosmetic_header_edits_still_pass(edit):
    check_table(_table(edit(TOTALS)), "totals table", columns=10, key_headers=KEYS)


def test_a_missing_column_fails_loudly():
    with pytest.raises(ParseError, match="9 columns, expected 10"):
        check_table(_table(TOTALS[:-1]), "totals table", columns=10, key_headers=KEYS)


def test_a_key_header_in_the_wrong_place_fails_loudly():
    swapped = ["KD", "Fighter", *TOTALS[2:]]

    with pytest.raises(ParseError, match="column 0"):
        check_table(_table(swapped), "totals table", columns=10, key_headers=KEYS)


def test_a_table_with_no_header_fails_loudly():
    table = BeautifulSoup("<table><tr><td>x</td></tr></table>", "lxml").table

    with pytest.raises(ParseError, match="0 columns"):
        check_table(table, "totals table", columns=10, key_headers=KEYS)
