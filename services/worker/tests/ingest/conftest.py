"""Fixtures for the ingestion tests: the scraper's saved ufcstats pages, parsed on demand."""

from pathlib import Path

import pytest

from cageops_scraper.parsers.event import parse_event
from cageops_scraper.parsers.fight import parse_fight
from cageops_scraper.parsers.fighter import parse_fighter

FIXTURES = Path(__file__).parents[3] / "scraper" / "tests" / "fixtures" / "ufcstats"
BASE = "http://ufcstats.com"

BURNS_CARD = "c3ac8d0da7b05772"  # UFC Fight Night: Burns vs. Malott, 2026-04-18
UPCOMING_CARD = "7f98d9d5a10fa25c"  # UFC Fight Night: Allen vs. Duncan, 2026-10-10


@pytest.fixture
def html():
    """Raw text of a saved page: html("fight_32054bf2b36b0e47")."""
    return lambda name: (FIXTURES / f"{name}.html").read_text(encoding="utf-8")


@pytest.fixture
def fight(html):
    return lambda fid: parse_fight(html(f"fight_{fid}"), f"{BASE}/fight-details/{fid}")


@pytest.fixture
def fighter(html):
    return lambda fid: parse_fighter(html(f"fighter_{fid}"), f"{BASE}/fighter-details/{fid}")


@pytest.fixture
def burns_card(html):
    return parse_event(html(f"event_{BURNS_CARD}"), f"{BASE}/event-details/{BURNS_CARD}")


@pytest.fixture
def upcoming_card(html):
    return parse_event(
        html(f"event_upcoming_{UPCOMING_CARD}"), f"{BASE}/event-details/{UPCOMING_CARD}"
    )


@pytest.fixture
def bout_for(burns_card):
    """The Burns card's event-page row for a fight id (None for fights not on this card)."""
    by_id = {b.fight_id: b for b in burns_card.bouts}
    return by_id.get
