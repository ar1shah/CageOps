"""A year of synthetic ufcstats pages, generated from our saved fixtures (for the replay server).

We hold 14 real pages and a benchmark needs about 680, so this clones real pages with new ids and
dates: real markup, invented data. It says nothing about the real site's data volume or speed
(see docs/BENCHMARKS.md for what the benchmark can and can't show).

One synthetic event is a copy of the Burns card cut down to the 4 bouts we hold full fight pages
for, so it is 1 event page + 4 fight pages + 8 fighter pages = 13 pages. Event k (0 = newest) is
dated k weeks before `end`. Every id in a /event-details/, /fight-details/ or /fighter-details/ URL
is mapped to sha1(id|k)[:16], the same way on every page of the event, so links stay consistent.
Pages are rendered on request, so the corpus holds ids, not megabytes of HTML. Same arguments give
byte-identical pages.

Fighters are never shared between events and fighter pages reuse the bios of the 4 real fighter
fixtures; both are limits stated next to the benchmark numbers.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

DEFAULT_FIXTURES = Path(__file__).parents[1] / "services/scraper/tests/fixtures/ufcstats"
DEFAULT_END = date(2026, 10, 3)  # the day the fixtures were saved
TEMPLATE_EVENT = "c3ac8d0da7b05772"  # UFC Fight Night: Burns vs. Malott, 2026-04-18
TEMPLATE_EVENT_NAME = "UFC Fight Night: Burns vs. Malott"
TEMPLATE_EVENT_DATE = "April 18, 2026"
TEMPLATE_FIGHTS = ("32054bf2b36b0e47", "552f7cdaf93e1055", "9fab4b0ad082f670", "b5299e5b946015e5")
FIGHTER_FIXTURES = (
    "23024fdfc966410a",
    "53e533db1b8e9712",
    "6cbb7661c3258617",
    "6eedb757f13b9978",
)
PAGE_SIZE = 25  # rows per events-list page, like the real list
ID_IN_URL = re.compile(r"(event|fight|fighter)-details/([0-9a-f]{16})")
EVENT_PATH = re.compile(r"/(event|fight|fighter)-details/([0-9a-f]{16})$")
LIST_PATH = re.compile(r"/statistics/events/completed(?:\?page=(\d+))?$")


def synthetic_id(template_id: str, k: int) -> str:
    return hashlib.sha1(f"{template_id}|{k}".encode()).hexdigest()[:16]


def _delete_bouts(event_html: str, keep: tuple[str, ...]) -> str:
    """The event page with every bout row removed except `keep` (delete-only, like the fixture
    minimizer)."""
    row = re.compile(r'<tr[^>]*data-link="[^"]*fight-details/([0-9a-f]{16})"[^>]*>.*?</tr>', re.S)

    def drop(match: re.Match[str]) -> str:
        return match.group(0) if match.group(1) in keep else ""

    return row.sub(drop, event_html)


def _fighter_ids(html: str) -> list[str]:
    seen: dict[str, None] = {}
    for kind, found in ID_IN_URL.findall(html):
        if kind == "fighter":
            seen.setdefault(found)
    return sorted(seen)


@dataclass(frozen=True)
class Page:
    kind: str  # event | fight | fighter
    k: int  # which synthetic event
    template_id: str


class Corpus:
    def __init__(
        self, events: int = 52, *, end: date = DEFAULT_END, fixtures: Path = DEFAULT_FIXTURES
    ):
        self.events, self.end, self._dir = events, end, fixtures
        read = self._read
        full_event = read(f"event_{TEMPLATE_EVENT}")
        self._event = _delete_bouts(full_event, TEMPLATE_FIGHTS)
        self._fights = {fid: read(f"fight_{fid}") for fid in TEMPLATE_FIGHTS}
        self._fighter_pages = [read(f"fighter_{fid}") for fid in FIGHTER_FIXTURES]
        # the 8 fighters on the cut-down card, each given one of the 4 real fighter pages
        self.template_fighters = _fighter_ids(self._event)
        self._fighter_page = {
            fid: self._fighter_pages[i % len(self._fighter_pages)]
            for i, fid in enumerate(self.template_fighters)
        }
        self._template_ids = (TEMPLATE_EVENT, *TEMPLATE_FIGHTS, *self.template_fighters)
        self._list = read("events_completed")
        self._index: dict[str, Page] = {}
        for k in range(events):
            self._index[synthetic_id(TEMPLATE_EVENT, k)] = Page("event", k, TEMPLATE_EVENT)
            for fid in TEMPLATE_FIGHTS:
                self._index[synthetic_id(fid, k)] = Page("fight", k, fid)
            for fid in self.template_fighters:
                self._index[synthetic_id(fid, k)] = Page("fighter", k, fid)

    def _read(self, name: str) -> str:
        return (self._dir / f"{name}.html").read_text(encoding="utf-8")

    # -- what the corpus contains ---------------------------------------------------------

    def event_date(self, k: int) -> date:
        return self.end - timedelta(weeks=k)

    def event_id(self, k: int) -> str:
        return synthetic_id(TEMPLATE_EVENT, k)

    def fight_ids(self, k: int) -> list[str]:
        return [synthetic_id(f, k) for f in TEMPLATE_FIGHTS]

    def fighter_ids(self, k: int) -> list[str]:
        return [synthetic_id(f, k) for f in self.template_fighters]

    def list_pages(self) -> int:
        """Pages that hold events: page 1 also carries the "next event" marker row."""
        return -(-(self.events + 1) // PAGE_SIZE)

    def paths(self) -> list[str]:
        """Every path a complete crawl requests (the empty page after the last one included)."""
        listing = ["/statistics/events/completed"] + [
            f"/statistics/events/completed?page={n}" for n in range(2, self.list_pages() + 2)
        ]
        return listing + [f"/{page.kind}-details/{i}" for i, page in self._index.items()]

    def expected_requests(self) -> int:
        """Pages one cold backfill of the whole corpus requests (not counting robots.txt)."""
        return self.events * (1 + len(TEMPLATE_FIGHTS) + len(self.template_fighters)) + (
            self.list_pages() + 1
        )

    # -- rendering ------------------------------------------------------------------------

    def render(self, path: str) -> str | None:
        """The page at a path (with its query string), or None. Links still point at
        http://ufcstats.com; the server rewrites them to its own address."""
        listing = LIST_PATH.search(path)
        if listing:
            return self._list_page(int(listing.group(1) or 1))
        match = EVENT_PATH.search(path)
        page = self._index.get(match.group(2)) if match else None
        if page is None or page.kind != match.group(1):
            return None
        return self._render(page)

    def _render(self, page: Page) -> str:
        if page.kind == "event":
            source = self._event
        elif page.kind == "fight":
            source = self._fights[page.template_id]
        else:
            source = self._fighter_page[page.template_id]
        html = self._remap(source, page.k)
        if page.kind == "event":
            html = self._rename_event(html, page.k)
        return html

    def _remap(self, html: str, k: int) -> str:
        known = set(self._template_ids)

        def swap(match: re.Match[str]) -> str:
            kind, found = match.groups()
            return f"{kind}-details/{synthetic_id(found, k) if found in known else found}"

        return ID_IN_URL.sub(swap, html)

    def _rename_event(self, html: str, k: int) -> str:
        d = self.event_date(k)
        html = html.replace(TEMPLATE_EVENT_DATE, f"{d:%B} {d.day}, {d.year}")
        return html.replace(TEMPLATE_EVENT_NAME, self.event_name(k))

    def event_name(self, k: int) -> str:
        return f"UFC Replay Night {self.events - k:02d}"

    # -- the events list ------------------------------------------------------------------

    def _list_rows(self) -> tuple[str, str, str, str]:
        """(before the rows, the marker row, the plain row, after the rows) from the real list."""
        start = self._list.index("<tbody>") + len("<tbody>")
        end = self._list.index("</tbody>")
        rows = re.findall(r"<tr class=.*?</tr>", self._list[start:end], re.S)
        marker = next(r for r in rows if "b-statistics__table-row_type_first" in r)
        plain = next(r for r in rows if "b-statistics__date" in r and r is not marker)
        clear = next(r for r in rows if "table-col_type_clear" in r)
        return self._list[:start] + "\n" + clear, marker, plain, self._list[end:]

    def _row(self, template: str, event_id: str, name: str, d: date) -> str:
        row = re.sub(r"event-details/[0-9a-f]{16}", f"event-details/{event_id}", template)
        row = re.sub(
            r"(<a href=[^>]*>\s*)(.*?)(\s*</a>)",
            lambda m: f"{m.group(1)}{name}{m.group(3)}",
            row,
            count=1,
            flags=re.S,
        )
        return re.sub(
            r'(<span class="b-statistics__date">\s*)(.*?)(\s*</span>)',
            lambda m: f"{m.group(1)}{d:%B %d, %Y}{m.group(3)}",
            row,
            count=1,
            flags=re.S,
        )

    def _list_page(self, number: int) -> str:
        head, marker, plain, tail = self._list_rows()
        upcoming = self._row(marker, "7f98d9d5a10fa25c", "UFC Fight Night: Allen vs. Duncan",
                             self.end + timedelta(days=7))  # fmt: skip
        everything = [upcoming] + [
            self._row(plain, self.event_id(k), self.event_name(k), self.event_date(k))
            for k in range(self.events)
        ]
        chunk = everything[(number - 1) * PAGE_SIZE : number * PAGE_SIZE]
        return head + "\n" + "\n".join(chunk) + "\n" + tail
