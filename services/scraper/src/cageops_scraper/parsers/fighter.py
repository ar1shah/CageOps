"""Parse a fighter page (/fighter-details/<id>): bio facts only.

The page also has career rates and a record "as of today". Reading them would let future fights
leak into features for past ones, so this parser never looks at them: it picks out four labelled
bio items (Height, Reach, Stance, DOB) and nothing else.
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from cageops_scraper.errors import ParseError
from cageops_scraper.parsers.common import (
    cell_text,
    load_soup,
    must_find,
    optional_text,
    parse_context,
    parse_date,
    parse_height_cm,
    parse_reach_cm,
    squash,
    ufcstats_id,
)
from cageops_scraper.parsers.models import FighterPage

_BIO_LABELS = ("height", "reach", "stance", "dob")


def parse_fighter(html: str, url: str) -> FighterPage:
    with parse_context(url):
        soup = load_soup(html)
        name = must_find(soup, "the fighter name", "span", class_="b-content__title-highlight")
        nickname = soup.find("p", class_="b-content__Nickname")
        bio = _bio_items(soup)
        return FighterPage(
            ufcstats_id=ufcstats_id(url),
            name=cell_text(name),
            nickname=optional_text(cell_text(nickname)) if nickname else None,
            height_cm=parse_height_cm(bio["height"]),
            reach_cm=parse_reach_cm(bio["reach"]),
            stance=optional_text(bio["stance"]),
            dob=parse_date(bio["dob"]),
        )


def _bio_items(soup: BeautifulSoup) -> dict[str, str]:
    """{'height': ..., 'reach': ..., 'stance': ..., 'dob': ...} as printed ('--' if blank).
    Every other labelled item on the page (weight, career rates) is ignored on purpose."""
    found: dict[str, str] = {}
    for item in soup.find_all("li", class_="b-list__box-list-item"):
        label = item.find("i", class_="b-list__box-item-title")
        if label is None:
            continue
        key = squash(label.get_text()).rstrip(":").lower()
        if key in _BIO_LABELS and key not in found:  # the first one is the bio list
            found[key] = squash(item.get_text(" ").replace(label.get_text(), "", 1))
    missing = [label for label in _BIO_LABELS if label not in found]
    if missing:
        raise ParseError(f"layout changed: the bio list has no {', '.join(missing)}")
    return found
