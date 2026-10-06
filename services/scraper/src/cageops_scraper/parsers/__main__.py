"""Eyeball a parser: print what it makes of a saved page, as JSON.

    uv run python -m cageops_scraper.parsers \
        services/scraper/tests/fixtures/ufcstats/fight_32054bf2b36b0e47.html

The kind (fight, fighter, event, events) and the page URL are worked out from the file name
(fight_<id>.html, fighter_<id>.html, event_<id>.html, events_completed.html); override them
with --kind and --url. Reads a file only: no network.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from cageops_scraper.errors import ParseError
from cageops_scraper.parsers.event import parse_event
from cageops_scraper.parsers.events import parse_events_list
from cageops_scraper.parsers.fight import parse_fight
from cageops_scraper.parsers.fighter import parse_fighter

BASE = "http://ufcstats.com"
PARSERS = {
    "fight": parse_fight,
    "fighter": parse_fighter,
    "event": parse_event,
    "events": parse_events_list,
}
_PATHS = {"fight": "fight-details", "fighter": "fighter-details", "event": "event-details"}


def guess(path: Path) -> tuple[str, str]:
    """(kind, url) for a fixture-style file name."""
    stem = path.stem
    if stem.startswith("events_"):
        return "events", f"{BASE}/statistics/events/{stem.removeprefix('events_')}"
    match = re.match(r"^(fight|fighter|event)(?:_upcoming)?_([0-9a-f]{16})$", stem)
    if not match:
        raise SystemExit(f"can't tell what {path.name} is; pass --kind and --url")
    return match.group(1), f"{BASE}/{_PATHS[match.group(1)]}/{match.group(2)}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("file", type=Path)
    parser.add_argument("--kind", choices=sorted(PARSERS))
    parser.add_argument("--url")
    args = parser.parse_args(argv)

    kind, url = guess(args.file) if not (args.kind and args.url) else (args.kind, args.url)
    kind, url = args.kind or kind, args.url or url
    try:
        result = PARSERS[kind](args.file.read_text(encoding="utf-8"), url)
    except ParseError as exc:
        print(f"ParseError: {exc}", file=sys.stderr)
        return 1
    payload = (
        [r.model_dump(mode="json") for r in result]
        if isinstance(result, list)
        else result.model_dump(mode="json")
    )
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
