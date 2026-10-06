"""A stand-in for ufcstats.com on your own machine: it serves our saved fixture pages.

For trying the ingestion pipeline end to end without sending a single request to the real site
(which is behind a bot challenge anyway, D-013). It is the seed of checkpoint 5's replay server.

    uv run python scripts/standin_site.py [--port 8099] [--fixtures DIR]

Pages are served at the real site's URL paths (/event-details/<id>, /fight-details/<id>, ...) so
the scraper can't tell the difference (links inside the pages are rewritten to point back here).
Anything we have no fixture for is a 404, like a page that
doesn't exist. Switch behaviour while it runs:

    curl -X POST localhost:8099/__mode/ok          pages as saved (the default)
    curl -X POST localhost:8099/__mode/fail        every content page answers 500
    curl -X POST localhost:8099/__mode/challenge   every content page is the bot-challenge page
    curl localhost:8099/__stats                    how many content pages were requested so far
    curl -X POST localhost:8099/__reset            zero that counter

For benchmarks it is the replay server: `--synthetic-events 52` serves a generated year of events
(scripts/replay_corpus.py) instead of the 14 saved pages, and `--latency-ms 250 --jitter 0.2` makes
every response take 250 ms +/- 20% (seeded, so a run is repeatable). The server is threaded, so
concurrent requests overlap like a real site's. Numbers measured against it are replay numbers,
never the real site's.

robots.txt always answers (allow everything), whatever the mode. Point the scraper at it with
SCRAPER_SOURCE=ufcstats_replay and UFCSTATS_REPLAY_BASE_URL=http://127.0.0.1:8099.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import threading
import time
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from replay_corpus import DEFAULT_END, Corpus

DEFAULT_FIXTURES = Path(__file__).parents[1] / "services/scraper/tests/fixtures/ufcstats"
ROBOTS = "User-agent: *\nAllow: /\n"
MODES = ("ok", "fail", "challenge")
CHALLENGE_FILE = "challenge_2026-10-03.html"
REAL_HOST = re.compile(r"https?://(?:www\.)?ufcstats\.com")


def empty_list(html: str) -> str:
    """A list page with no events: what a page past the end of the history looks like."""
    start, end = html.index("<tbody>"), html.index("</tbody>")
    return html[:start] + "<tbody></tbody>" + html[end + len("</tbody>") :]


def load_pages(fixtures: Path) -> dict[str, str]:
    """{path-and-query: html}, read from the fixture files by their names."""
    pages: dict[str, str] = {}

    def read(name: str) -> str:
        return (fixtures / f"{name}.html").read_text(encoding="utf-8")

    completed = read("events_completed")
    pages["/statistics/events/completed"] = completed
    # Every later page is empty, so a crawl that follows "next page" stops after page 1.
    for page in range(2, 6):
        pages[f"/statistics/events/completed?page={page}"] = empty_list(completed)
    pages["/statistics/events/upcoming"] = read("events_upcoming")

    patterns = (
        (r"event_upcoming_(\w+)", "/event-details/{}"),
        (r"event_(\w+)", "/event-details/{}"),
        (r"fight_(\w+)", "/fight-details/{}"),
        (r"fighter_(\w+)", "/fighter-details/{}"),
    )
    for path in sorted(fixtures.glob("*.html")):
        for pattern, route in patterns:
            match = re.fullmatch(pattern, path.stem)
            if match:
                pages[route.format(match.group(1))] = path.read_text(encoding="utf-8")
                break
    return pages


class StandIn:
    """The state behind the handler: pages, the current mode, latency and request counters."""

    def __init__(
        self,
        fixtures: Path = DEFAULT_FIXTURES,
        *,
        synthetic_events: int = 0,
        corpus_end: date | None = None,
        latency_ms: float = 0.0,
        jitter: float = 0.2,
        seed: int = 0,
    ):
        self.pages = load_pages(fixtures)
        self.corpus = (
            Corpus(synthetic_events, end=corpus_end or DEFAULT_END, fixtures=fixtures)
            if synthetic_events
            else None
        )
        self.challenge = (fixtures / CHALLENGE_FILE).read_text(encoding="utf-8")
        self.latency_ms, self.jitter = latency_ms, jitter
        self.base_url = ""  # set once the port is known (make_server)
        self.mode = "ok"
        self.quiet = False
        self.requests = 0
        self.distinct: set[str] = set()
        self.lock = threading.Lock()
        self._rng = random.Random(seed)

    def delay_s(self) -> float:
        """How long the next response takes: latency +/- jitter, from a seeded generator."""
        if not self.latency_ms:
            return 0.0
        with self.lock:
            factor = 1 + self._rng.uniform(-self.jitter, self.jitter)
        return self.latency_ms * factor / 1000

    def page(self, path: str) -> str | None:
        """The saved or synthetic page at a path. With a synthetic corpus, the completed list and
        every event, fight and fighter come from it; the upcoming list and event stay as saved."""
        if self.corpus is not None:
            html = self.corpus.render(path)
            if html is not None:
                return html
            if path.startswith("/statistics/events/completed"):
                return None  # beyond the corpus
        return self.pages.get(path)

    def respond(self, path: str) -> tuple[int, str]:
        if path == "/robots.txt":
            return 200, ROBOTS
        with self.lock:
            self.requests += 1
            self.distinct.add(path)
            mode = self.mode
        if mode == "fail":
            return 500, "Internal Server Error"
        if mode == "challenge":
            return 200, self.challenge
        html = self.page(path)
        if html is not None:
            return 200, self.relink(html)
        return 404, "Not Found"

    def relink(self, html: str) -> str:
        """The saved pages link to http://ufcstats.com/...; point those links at this server.
        (The scraper rightly refuses a link to a different host than the source it is reading.)"""
        return REAL_HOST.sub(self.base_url, html) if self.base_url else html


def make_handler(site: StandIn) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: str, content_type: str = "text/html; charset=utf-8"):
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _json(self, status: int, data: dict) -> None:
            self._send(status, json.dumps(data), "application/json")

        def do_GET(self) -> None:
            split = urlsplit(self.path)
            if split.path == "/__stats":
                self._json(
                    200,
                    {
                        "mode": site.mode,
                        "requests": site.requests,
                        "distinct_pages": len(site.distinct),
                        "latency_ms": site.latency_ms,
                    },
                )
                return
            full = split.path + (f"?{split.query}" if split.query else "")
            time.sleep(site.delay_s())  # the simulated round trip; other requests carry on
            status, body = site.respond(full)
            if not site.quiet:
                print(f"{status} {full}", file=sys.stderr, flush=True)
            self._send(status, body)

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            if path.startswith("/__mode/") and path.removeprefix("/__mode/") in MODES:
                site.mode = path.removeprefix("/__mode/")
                print(f"-- mode is now {site.mode}", file=sys.stderr, flush=True)
                self._json(200, {"mode": site.mode})
            elif path == "/__reset":
                with site.lock:
                    site.requests = 0
                    site.distinct.clear()
                self._json(200, {"requests": 0})
            else:
                self._json(404, {"error": f"unknown control path; modes are {', '.join(MODES)}"})

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            pass  # we print our own, shorter line

    return Handler


class Server(ThreadingHTTPServer):
    request_queue_size = 128  # many workers connect at once; the default backlog is 5


def make_server(
    port: int, fixtures: Path = DEFAULT_FIXTURES, **site_options
) -> tuple[ThreadingHTTPServer, StandIn]:
    site = StandIn(fixtures, **site_options)
    server = Server(("127.0.0.1", port), make_handler(site))
    server.daemon_threads = True
    site.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    return server, site


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    parser.add_argument(
        "--synthetic-events", type=int, default=0, metavar="N",
        help="serve N generated weekly events (13 pages each) instead of the saved pages",
    )  # fmt: skip
    parser.add_argument(
        "--corpus-end", type=date.fromisoformat, metavar="YYYY-MM-DD",
        help="date of the newest synthetic event (default: the day the fixtures were saved)",
    )  # fmt: skip
    parser.add_argument("--latency-ms", type=float, default=0.0, help="simulated response time")
    parser.add_argument(
        "--jitter", type=float, default=0.2, help="latency varies by +/- this share"
    )
    parser.add_argument("--seed", type=int, default=0, help="seed for the latency jitter")
    parser.add_argument("--quiet", action="store_true", help="don't print a line per request")
    args = parser.parse_args(argv)
    server, site = make_server(
        args.port,
        args.fixtures,
        synthetic_events=args.synthetic_events,
        corpus_end=args.corpus_end,
        latency_ms=args.latency_ms,
        jitter=args.jitter,
        seed=args.seed,
    )
    site.quiet = args.quiet
    print(
        f"stand-in ufcstats on {site.base_url} "
        f"({len(site.pages)} saved pages"
        + (f", {site.corpus.events} synthetic events" if site.corpus else "")
        + f", latency {args.latency_ms:g} ms, mode ok). Ctrl-C to stop.",
        file=sys.stderr,
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
