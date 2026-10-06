"""Shrink a saved Wikipedia page into a committable test fixture (D-029).

    uv run python scripts/minimize_wikipedia_fixture.py data/fixtures/wikipedia/UFC_332.html \\
        --out services/scraper/tests/fixtures/wikipedia

The full pages stay in data/ (gitignored). Only the trimmed copies are committed, and the text of
Wikipedia articles is CC BY-SA 4.0, so the fixtures directory carries its own NOTICE.

What is kept, each node copied as it was saved (its tags, attributes and text, re-serialized by
BeautifulSoup, not byte-identical), everything else dropped:
- <title>, the canonical <link>, and the inline <script> that carries wgArticleId/wgRevisionId
- an event article: the <h1>, the infobox, the "Results" heading and its table(s), and only the
  footnotes those tables point at (the note text that says "For the UFC ... Championship")
- a "{year} in UFC" page: the <h1>, the "Past events" heading and its table
Prose, images, navigation, references and every other table are removed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from bs4 import BeautifulSoup, Tag


def config_script(soup: BeautifulSoup) -> Tag:
    for script in soup.find_all("script"):
        if "wgArticleId" in (script.string or ""):
            return script
    raise SystemExit("no inline script with wgArticleId: not a Wikipedia article page?")


def event_nodes(soup: BeautifulSoup) -> list[Tag]:
    heading = soup.find(id="Results")
    if heading is None:
        raise SystemExit("no Results heading")
    div = heading.parent  # <div class="mw-heading mw-heading2"><h2 id="Results">
    tables = []
    for sibling in div.next_siblings:
        if not isinstance(sibling, Tag):
            continue
        if "mw-heading" in (sibling.get("class") or []):
            break  # the next section
        if sibling.name == "table":
            tables.append(sibling)
    if not tables:
        raise SystemExit("no results table after the Results heading")
    notes = []
    for table in tables:
        for link in table.select('a[href^="#cite_note-"]'):
            note = soup.find(id=str(link["href"])[1:])
            if note is not None and note not in notes:
                notes.append(note)
    infobox = soup.select_one("table.infobox")
    nodes: list[Tag] = [infobox] if infobox else []
    nodes += [div, *tables]
    if notes:
        references = soup.new_tag("ol", attrs={"class": "references"})
        for note in notes:
            references.append(BeautifulSoup(str(note), "lxml").li)
        nodes.append(references)
    return nodes


def year_list_nodes(soup: BeautifulSoup) -> list[Tag]:
    heading = soup.find(id="Past_events")
    if heading is None:
        raise SystemExit("no Past events heading")
    table = heading.find_next("table", class_="wikitable")
    return [heading.parent, table]


def minimize(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    is_year_list = soup.find(id="Past_events") is not None
    nodes = year_list_nodes(soup) if is_year_list else event_nodes(soup)

    out = BeautifulSoup("<!DOCTYPE html><html><head></head><body></body></html>", "lxml")
    out.head.append(BeautifulSoup(str(soup.title), "lxml").title)
    canonical = soup.find("link", rel="canonical")
    if canonical is not None:
        out.head.append(BeautifulSoup(str(canonical), "lxml").link)
    out.head.append(BeautifulSoup(str(config_script(soup)), "lxml").script)
    h1 = soup.select_one("h1")
    if h1 is not None:
        out.body.append(BeautifulSoup(str(h1), "lxml").h1)
    for node in nodes:
        out.body.append(BeautifulSoup(str(node), "lxml").body.contents[0])
    return str(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("pages", nargs="+", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    for page in args.pages:
        original = page.read_text(encoding="utf-8")
        trimmed = minimize(original)
        target = args.out / page.name
        target.write_text(trimmed, encoding="utf-8")
        print(f"{page.name}: {len(original):>9,} -> {len(trimmed):>8,} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
