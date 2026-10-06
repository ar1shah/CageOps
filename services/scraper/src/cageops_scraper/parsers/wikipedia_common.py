"""Building blocks for the Wikipedia parsers. Pure, like parsers/common.py."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlsplit

from bs4 import BeautifulSoup, Tag

from cageops_scraper.errors import ParseError
from cageops_scraper.parsers.common import squash

_ARTICLE_ID = re.compile(r'"wgArticleId":(\d+)')
_REVISION_ID = re.compile(r'"wgRevisionId":(\d+)')
# Footnote markers render as "[a]" or "[ a ]" or "[1]"; champion markers as "(c)" and "(ic)".
FOOTNOTE_MARKER = re.compile(r"\[\s*[A-Za-z0-9]{1,3}\s*\]")
CHAMPION_MARKER = re.compile(r"\(\s*(?:c|ic)\s*\)", re.IGNORECASE)


def load_wikipedia_soup(html: str) -> BeautifulSoup:
    if not html or not html.strip():
        raise ParseError("empty page")
    return BeautifulSoup(html, "lxml")


def page_ids(html: str) -> tuple[int, int]:
    """(wgArticleId, wgRevisionId) from the page's inline config. Both must be there."""
    article, revision = _ARTICLE_ID.search(html), _REVISION_ID.search(html)
    if article is None or revision is None:
        raise ParseError("layout changed: the page config has no wgArticleId / wgRevisionId")
    return int(article.group(1)), int(revision.group(1))


def clean_cell_text(text: str) -> str:
    """Cell text with footnote markers removed and whitespace collapsed (champion markers stay)."""
    return squash(FOOTNOTE_MARKER.sub(" ", text))


def strip_champion_marker(text: str) -> tuple[str, bool]:
    return squash(CHAMPION_MARKER.sub(" ", text)), CHAMPION_MARKER.search(text) is not None


def link_title(cell: Tag) -> str | None:
    """The article an in-cell link points at: /wiki/<Title>, or the title= of a red link
    (a page that doesn't exist yet). None if the cell has no such link."""
    for anchor in cell.find_all("a", href=True):
        href = str(anchor["href"])
        parts = urlsplit(href)
        if parts.path.startswith("/wiki/"):
            title = unquote(parts.path.removeprefix("/wiki/"))
        elif parts.path.endswith("/index.php") and "redlink" in parts.query:
            values = parse_qs(parts.query).get("title")
            title = values[0] if values else ""
        else:
            continue
        title = title.replace(" ", "_").strip("_")
        if title and ":" not in title.split("_")[0]:  # not File:, Category:, ...
            return title
    return None


def title_from_href(href: str) -> str | None:
    parts = urlsplit(href)
    if not parts.path.startswith("/wiki/"):
        return None
    title = unquote(parts.path.removeprefix("/wiki/")).replace(" ", "_").strip("_")
    return title or None
