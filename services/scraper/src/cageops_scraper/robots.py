"""robots.txt check, run at every scraper start (the "read the sign before walking in" step).

Status handling follows RFC 9309:
- 2xx: parse it; refuse to run if a path we need is disallowed for our User-Agent.
- 4xx (ufcstats today: 404): no rules published, so allowed. Our own rate limit still applies.
- 5xx or unreachable: we can't tell, so assume disallowed and refuse to run.
A Crawl-delay longer than our default replaces the default.
"""

from __future__ import annotations

import contextlib
import math
from collections.abc import Callable
from dataclasses import dataclass
from urllib.robotparser import RobotFileParser

from cageops_scraper.errors import (
    NotFound,
    PermanentFetchError,
    RetryableFetchError,
    RobotsDisallowed,
    RobotsUnavailable,
)
from cageops_scraper.page import Page
from cageops_scraper.sources.base import Source


@dataclass(frozen=True)
class RobotsResult:
    outcome: str  # "no_rules" (4xx) or "parsed"
    crawl_delay_s: float | None
    interval_ms: int  # the minimum spacing between requests we will actually use


def check_robots(
    source: Source,
    user_agent: str,
    fetch_robots: Callable[[str], Page],
    default_interval_ms: int,
) -> RobotsResult:
    robots_url = f"{source.base_url}/robots.txt"
    try:
        page = fetch_robots(robots_url)
    except NotFound:
        return RobotsResult("no_rules", None, default_interval_ms)
    except PermanentFetchError as exc:
        if exc.status is not None and 400 <= exc.status < 500:
            return RobotsResult("no_rules", None, default_interval_ms)
        raise RobotsUnavailable(f"robots.txt gave an unexpected answer: {exc}") from exc
    except RetryableFetchError as exc:
        raise RobotsUnavailable(f"could not read robots.txt, so not crawling: {exc}") from exc

    parser = RobotFileParser()
    parser.parse(page.html.splitlines())
    parser.modified()  # parse() alone leaves "never checked", which can_fetch() treats as deny
    for path in source.required_paths:
        if not parser.can_fetch(user_agent, f"{source.base_url}{path}"):
            raise RobotsDisallowed(path)

    delay = crawl_delay(page.html, user_agent)
    interval_ms = default_interval_ms
    if delay is not None:
        interval_ms = max(default_interval_ms, math.ceil(delay * 1000))
    return RobotsResult("parsed", delay, interval_ms)


def crawl_delay(robots_txt: str, user_agent: str) -> float | None:
    """The Crawl-delay (seconds) that applies to us, or None.

    The standard library's parser only reads whole-number delays, so "Crawl-delay: 1.5" would
    be ignored. This reads fractions. As in the spec, the most specific matching group wins
    and groups don't inherit from "*".
    """
    groups: list[tuple[list[str], float | None]] = []
    agents: list[str] = []
    delay: float | None = None
    in_rules = False
    for raw in robots_txt.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        field, value = (part.strip() for part in line.split(":", 1))
        field = field.lower()
        if field == "user-agent":
            if in_rules:  # a new group starts after the previous group's rules
                groups.append((agents, delay))
                agents, delay, in_rules = [], None, False
            agents.append(value.lower())
        elif agents:
            in_rules = True
            if field == "crawl-delay":
                with contextlib.suppress(ValueError):  # junk value: ignore the line
                    delay = float(value)
    if agents:
        groups.append((agents, delay))

    ua = user_agent.lower()
    specific = [d for names, d in groups if any(n != "*" and n in ua for n in names)]
    if specific:
        return max((d for d in specific if d is not None), default=None)
    star = [d for names, d in groups if "*" in names]
    return max((d for d in star if d is not None), default=None)
