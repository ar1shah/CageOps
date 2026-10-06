"""Parse an event article: the infobox (name, date) and the Results table (every bout)."""

from __future__ import annotations

import re
from datetime import date

from bs4 import BeautifulSoup, Tag

from cageops_scraper.errors import ParseError
from cageops_scraper.parsers.common import cell_text, must_find, parse_context, parse_int
from cageops_scraper.parsers.wikipedia_common import (
    clean_cell_text,
    link_title,
    load_wikipedia_soup,
    page_ids,
    strip_champion_marker,
    title_from_href,
)
from cageops_scraper.parsers.wikipedia_models import NoteKind, WikiBout, WikiEventPage, WikiFighter

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_CHAMPIONSHIP = re.compile(r"\bUFC\b.*\bChampionship\b", re.IGNORECASE)
COLUMNS = 8  # weight class, winner, def./vs., loser, method, round, time, notes
# Loose layout check: a few headers by position, lowercased ("contains").
_HEADERS = {0: "weight class", 4: "method", 5: "round", 6: "time", 7: "notes"}


def parse_event(html: str, url: str) -> WikiEventPage:
    with parse_context(url):
        soup = load_wikipedia_soup(html)
        article_id, revision_id = page_ids(html)
        bouts, anomalies = _parse_results(soup)
        if not bouts:
            raise ParseError("layout changed: the results table has no bouts")
        return WikiEventPage(
            name=_event_name(soup),
            event_date=_event_date(soup),
            article_id=article_id,
            revision_id=revision_id,
            canonical_title=_canonical_title(soup),
            bouts=tuple(bouts),
            anomalies=tuple(anomalies),
        )


# -- the infobox and the page head -----------------------------------------------------------


def _infobox(soup: BeautifulSoup) -> Tag:
    return must_find(soup, "the infobox", "table", class_="infobox")


def _event_name(soup: BeautifulSoup) -> str:
    above = _infobox(soup).find(class_="infobox-above")
    name = cell_text(above) if isinstance(above, Tag) else ""
    if not name:
        heading = must_find(soup, "the page heading", "h1")
        name = cell_text(heading)
    return name


def _event_date(soup: BeautifulSoup) -> date:
    for row in _infobox(soup).find_all("tr"):
        label = row.find("th")
        if label is not None and cell_text(label) == "Date":
            iso = _ISO_DATE.search(cell_text(row.find("td") or label))
            if iso is None:
                raise ParseError("layout changed: the infobox date has no ISO date")
            return date.fromisoformat(iso.group(0))
    raise ParseError("layout changed: the infobox has no Date row")


def _canonical_title(soup: BeautifulSoup) -> str:
    link = soup.find("link", rel="canonical")
    title = (
        title_from_href(str(link["href"])) if isinstance(link, Tag) and link.get("href") else None
    )
    if title is None:
        raise ParseError("layout changed: the page has no canonical link")
    return title


# -- the results table ---------------------------------------------------------------------------


def _results_tables(soup: BeautifulSoup) -> list[Tag]:
    heading = must_find(soup, "the Results heading", id="Results")
    section = heading.parent  # <div class="mw-heading mw-heading2"> in the saved pages
    tables = []
    for sibling in section.next_siblings:
        if not isinstance(sibling, Tag):
            continue
        if "mw-heading" in (sibling.get("class") or []):
            break  # the next section
        if sibling.name == "table":
            tables.append(sibling)
    if not tables:
        raise ParseError("layout changed: no table after the Results heading")
    return tables


def _parse_results(soup: BeautifulSoup) -> tuple[list[WikiBout], list[str]]:
    bouts: list[WikiBout] = []
    anomalies: list[str] = []
    for table in _results_tables(soup):
        _check_headers(table)
        for row in table.find_all("tr"):
            cells = row.find_all(["th", "td"])
            if cells[0].name == "th":
                continue  # "Main card", "Preliminary card" and the column headers
            if len(cells) != COLUMNS:
                raise ParseError(
                    f"layout changed: a bout row has {len(cells)} cells, not {COLUMNS}"
                )
            bout, found = _parse_bout(soup, cells)
            bouts.append(bout)
            anomalies += found
    return bouts, anomalies


def _check_headers(table: Tag) -> None:
    header = next(
        (r for r in table.find_all("tr") if r.find("th") and len(r.find_all("th")) == COLUMNS), None
    )
    if header is None:
        raise ParseError("layout changed: the results table has no column header row")
    texts = [cell_text(th).lower() for th in header.find_all("th")]
    for position, wanted in _HEADERS.items():
        if wanted not in texts[position]:
            raise ParseError(f"layout changed: results column {position} is {texts[position]!r}")


def _parse_fighter(cell: Tag) -> WikiFighter:
    name, marker = strip_champion_marker(clean_cell_text(cell.get_text(" ")))
    if not name:
        raise ParseError("layout changed: a bout has an empty fighter name")
    return WikiFighter(name=name, link_title=link_title(cell), champion_marker=marker)


def _footnotes(soup: BeautifulSoup, *cells: Tag) -> list[str]:
    """The text of every footnote the given cells point at (read, never stored)."""
    texts = []
    for cell in cells:
        for anchor in cell.select('a[href^="#cite_note-"]'):
            note = soup.find(id=str(anchor["href"])[1:])
            text = note.find(class_="reference-text") if isinstance(note, Tag) else None
            if isinstance(text, Tag):
                texts.append(cell_text(text))
    return texts


def note_kind(texts: list[str]) -> NoteKind:
    joined = " ".join(texts)
    if "BMF" in joined:
        return "bmf"
    return "championship" if _CHAMPIONSHIP.search(joined) else None


def _parse_bout(soup: BeautifulSoup, cells: list[Tag]) -> tuple[WikiBout, list[str]]:
    weight, first, versus, second, method, rnd, clock, notes = cells
    token = cell_text(versus)
    if token not in ("def.", "vs."):
        raise ParseError(f"expected 'def.' or 'vs.' between the fighters, found {token!r}")
    left, right = _parse_fighter(first), _parse_fighter(second)
    kind = note_kind(_footnotes(soup, notes, method) + [clean_cell_text(notes.get_text(" "))])
    anomalies = []
    if (left.champion_marker or right.champion_marker) and kind != "championship":
        anomalies.append(f"title_marker_without_note:{left.name} vs {right.name}")
    method_text = clean_cell_text(method.get_text(" "))
    if not method_text:
        raise ParseError(f"{left.name} vs {right.name}: the method is blank")
    time_text = clean_cell_text(cell_text(cells[6]))
    return (
        WikiBout(
            weight_class_raw=clean_cell_text(weight.get_text(" ")),
            first=left,
            second=right,
            versus=token,
            method_raw=method_text,
            round=parse_int(clean_cell_text(rnd.get_text(" "))),
            time_raw=time_text or None,
            note_kind=kind,
        ),
        anomalies,
    )
