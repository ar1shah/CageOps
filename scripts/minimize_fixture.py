"""Shrink a hand-saved ufcstats page into a committable test fixture (D-017).

    uv run python scripts/minimize_fixture.py data/fixtures/ufcstats/*.html \\
        --out services/scraper/tests/fixtures/ufcstats

The full pages stay in data/ (gitignored). Only the minimized copies are committed.

The one rule: this script only DELETES. It finds where the unwanted nodes start and end in the
original text and cuts those character ranges out; every character that remains is exactly as
it was saved (same tags, attribute order and quoting, classes, entities, whitespace, line
endings). Nothing is re-serialized, because a parser that rebuilds the HTML would quietly
change it and the fixture would stop being what the scraper would really receive. Before
writing, the result is checked to be the original with ranges removed.

What gets removed by default: scripts, styles, noscript, iframes, ad slots (<ins>), inline
SVG, nav, footer, <link> tags and HTML comments. Add more once the parsers show what they never
read, e.g. --remove-class b-statistics__sidebar. Nothing the page's content lives in
(headings, tables, lists, text) is on the default list.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

from cageops_scraper.sources import UfcStatsSource

DEFAULT_REMOVE_TAGS = frozenset(
    {"script", "style", "noscript", "iframe", "ins", "svg", "nav", "footer", "link"}
)
VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param"}
    | {"source", "track", "wbr"}
)


class MinimizeError(Exception):
    pass


@dataclass(frozen=True)
class Minimized:
    html: str
    removed: Counter[str]  # what was cut, by tag name ("comment" for comments)


class _RangeFinder(HTMLParser):
    """Finds the character ranges to delete. Never builds output of its own."""

    def __init__(
        self, text: str, tags: frozenset[str], classes: frozenset[str], ids: frozenset[str]
    ):
        super().__init__(convert_charrefs=False)  # keep positions exact
        self.text = text
        self.tags, self.classes, self.ids = tags, classes, ids
        self.ranges: list[tuple[int, int]] = []
        self.removed: Counter[str] = Counter()
        self._line_starts = [0] + [i + 1 for i, ch in enumerate(text) if ch == "\n"]
        # The outermost element being removed: [tag, start offset, nesting depth of that tag]
        self._active: list | None = None

    def _offset(self) -> int:
        line, col = self.getpos()
        return self._line_starts[line - 1] + col

    def _wanted(self, tag: str, attrs: list[tuple[str, str | None]]) -> bool:
        if tag in self.tags:
            return True
        values = dict(attrs)
        if self.ids and values.get("id") in self.ids:
            return True
        return bool(self.classes & set((values.get("class") or "").split()))

    def handle_starttag(self, tag, attrs):
        if self._active:
            if tag == self._active[0]:
                self._active[2] += 1
            return
        if not self._wanted(tag, attrs):
            return
        start = self._offset()
        if tag in VOID_TAGS:
            self._cut(tag, start, start + len(self.get_starttag_text()))
        else:
            self._active = [tag, start, 1]

    def handle_startendtag(self, tag, attrs):  # <svg/>, <path .../>: no end tag to wait for
        if not self._active and self._wanted(tag, attrs):
            start = self._offset()
            self._cut(tag, start, start + len(self.get_starttag_text()))

    def handle_endtag(self, tag):
        if self._active and tag == self._active[0]:
            self._active[2] -= 1
            if self._active[2] == 0:
                start = self._offset()
                close = self.text.find(">", start)
                self._cut(tag, self._active[1], close + 1)
                self._active = None

    def handle_comment(self, data):
        if not self._active:
            start = self._offset()
            self._cut("comment", start, self.text.index("-->", start) + 3)

    def _cut(self, name: str, start: int, end: int) -> None:
        self.ranges.append((start, end))
        self.removed[name] += 1

    def finish(self) -> None:
        self.close()
        if self._active:
            raise MinimizeError(f"<{self._active[0]}> was never closed; refusing to guess")


def is_pure_deletion(original: str, result: str) -> bool:
    """True if `result` is `original` with some characters deleted and nothing else changed."""
    remaining = iter(original)
    return all(ch in remaining for ch in result)  # each char must be found, in order


def minimize(
    html: str,
    remove_tags: frozenset[str] = DEFAULT_REMOVE_TAGS,
    remove_classes: frozenset[str] = frozenset(),
    remove_ids: frozenset[str] = frozenset(),
) -> Minimized:
    if UfcStatsSource().block_reason(200, html) is not None:
        raise MinimizeError("this is the bot-challenge page, not a real page; save it again")
    finder = _RangeFinder(html, remove_tags, remove_classes, remove_ids)
    finder.feed(html)
    finder.finish()

    pieces, cursor = [], 0
    for start, end in sorted(finder.ranges):
        pieces.append(html[cursor:start])
        cursor = max(cursor, end)
    pieces.append(html[cursor:])
    result = "".join(pieces)
    if not is_pure_deletion(html, result):  # can't happen by construction; checked anyway
        raise MinimizeError("internal error: output is not the input with ranges removed")
    return Minimized(result, finder.removed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("inputs", nargs="+", type=Path, help="full pages saved by hand")
    parser.add_argument("--out", type=Path, required=True, help="directory for minimized copies")
    parser.add_argument("--remove-tag", action="append", default=[], help="also remove this tag")
    parser.add_argument(
        "--remove-class", action="append", default=[], help="also remove elements with this class"
    )
    parser.add_argument(
        "--remove-id", action="append", default=[], help="also remove the element with this id"
    )
    args = parser.parse_args(argv)

    tags = DEFAULT_REMOVE_TAGS | {t.lower() for t in args.remove_tag}
    failed = False
    args.out.mkdir(parents=True, exist_ok=True)
    for path in args.inputs:
        target = args.out / path.name
        if target.resolve() == path.resolve():
            print(f"{path}: refusing to overwrite the original", file=sys.stderr)
            failed = True
            continue
        try:
            with path.open(encoding="utf-8", newline="") as handle:  # newline="": keep \r\n
                original = handle.read()
            result = minimize(
                original, tags, frozenset(args.remove_class), frozenset(args.remove_id)
            )
        except (MinimizeError, UnicodeDecodeError) as exc:
            print(f"{path}: {exc}", file=sys.stderr)
            failed = True
            continue
        with target.open("w", encoding="utf-8", newline="") as handle:
            handle.write(result.html)
        cut = ", ".join(f"{n} {name}" for name, n in sorted(result.removed.items())) or "nothing"
        before, after = len(original.encode()), len(result.html.encode())
        print(f"{path.name}: {before:,} -> {after:,} bytes (removed {cut})")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
