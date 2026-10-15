"""What an UPCOMING ufcstats fight-details page carries, pinned (Gate G, D-044).

Two pages saved by hand on 2026-10-15, one title bout and one not. They have the bout heading and
a belt image for a title bout, and NO round count: "Time format" and the whole result block
(Method, Round, Time, Referee) are absent, because that block exists only after the fight. So
`scheduled_rounds` has no source on ufcstats before a fight, and the scheduled-fight job that
would have written `is_title_fight` / `scheduled_rounds` was not built (D-044).

If one of these tests fails, ufcstats changed its upcoming page. That is news: re-read D-044 and
decide whether the job is now worth building, rather than editing the test to pass.
"""

import re
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from cageops_scraper.errors import ParseError
from cageops_scraper.parsers.fight import parse_fight

PAGES = Path(__file__).parent.parent / "fixtures" / "ufcstats" / "upcoming_fight_pages"
TITLE_BOUT = "ce1388d6c7651d2e"  # Volkanovski vs Evloev, UFC Featherweight Title Bout
OTHER_BOUT = "b9004bd72276a190"  # Kavanagh vs Temirov, Flyweight Bout


def soup(fight_id: str) -> BeautifulSoup:
    return BeautifulSoup(
        (PAGES / f"fight_upcoming_{fight_id}.html").read_text("utf-8"), "html.parser"
    )


def heading(page: BeautifulSoup) -> str:
    node = page.find("i", class_="b-fight-details__fight-title")
    assert node is not None, "the bout heading element is gone"
    return " ".join(node.get_text(" ").split())


def test_the_title_bout_says_so_in_its_heading_and_with_a_belt():
    page = soup(TITLE_BOUT)

    assert heading(page) == "UFC Featherweight Title Bout"
    assert page.find("img", src=re.compile(r"belt\.png")) is not None


def test_the_other_bout_has_a_plain_heading_and_no_belt():
    page = soup(OTHER_BOUT)

    assert heading(page) == "Flyweight Bout"
    assert page.find("img", src=re.compile(r"belt\.png")) is None


@pytest.mark.parametrize("fight_id", [TITLE_BOUT, OTHER_BOUT])
def test_neither_page_has_a_round_count_or_a_result_block(fight_id):
    page = soup(fight_id)
    text = " ".join(page.get_text(" ").split())

    for absent in ("Time format", "Rnd", "Referee", "Method:", "Round:"):
        assert absent not in text, f"{absent!r} is now on an upcoming page: revisit D-044"
    assert re.search(r"\b[35] Rnd\b|\(5-5-5", text) is None
    assert page.find(class_="b-fight-details__text") is None  # the result block's container


@pytest.mark.parametrize("fight_id", [TITLE_BOUT, OTHER_BOUT])
def test_the_fight_parser_cannot_read_an_upcoming_page(fight_id):
    """So a title-only job would need its own parser, not just a new job."""
    html = (PAGES / f"fight_upcoming_{fight_id}.html").read_text("utf-8")

    with pytest.raises(ParseError, match="unknown result badge"):
        parse_fight(html, f"http://ufcstats.com/fight-details/{fight_id}")
