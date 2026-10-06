"""English Wikipedia as a Source: event articles, fetched as plain /wiki/<Title> pages.

Why only /wiki/ article pages: robots.txt disallows /w/ (which includes /w/api.php) and /api/ for
generic agents, and our rule is that we obey robots.txt. So `canonical_url` makes anything else
impossible: no /w/, no /api/, no query string, no Special: or other non-article namespace.

The text is CC BY-SA 4.0; we store facts only and credit the article by URL (D-028, D-029).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from cageops_scraper.sources.base import CachePolicy, PageKind, SlowResponse

REAL_HOST = "en.wikipedia.org"

# A fresh result can be edited (or vandalised) at any time, so an event article is refetched daily
# for 30 days after the event, then treated as final. Anything older needs --force.
EVENT_TTL = timedelta(hours=24)
FINAL_AFTER_EVENT = timedelta(days=30)
YEAR_LIST_TTL = timedelta(hours=6)

# The Robot policy asks for 5 seconds' pause after a request that took over 1 second to serve. It
# is written for the Action API; we apply it to article pages too because it costs almost nothing.
SLOW_RESPONSE = SlowResponse(threshold_s=1.0, pause_s=5.0)

# Namespaces that are not articles. robots.txt disallows several of them, and none holds results.
_NOT_ARTICLES = frozenset(
    {
        "special", "talk", "user", "user_talk", "wikipedia", "wikipedia_talk", "file",
        "file_talk", "mediawiki", "mediawiki_talk", "template", "template_talk", "help",
        "help_talk", "category", "category_talk", "portal", "portal_talk", "draft",
        "draft_talk", "module", "module_talk", "timedtext", "timedtext_talk", "topic",
    }
)  # fmt: skip
# Wikipedia leaves these unescaped in a title; everything else (spaces, non-ASCII) is escaped.
_TITLE_SAFE = ":@!$&'()*+,;=-._~/"


class WikipediaSource:
    name = "wikipedia"
    required_paths = ("/wiki/2026_in_UFC",)
    slow_response = SLOW_RESPONSE

    def __init__(self, base_url: str = f"https://{REAL_HOST}"):
        self.base_url = base_url.rstrip("/")
        self._parts = urlsplit(self.base_url)
        if self._parts.netloc.lower() != REAL_HOST:
            raise ValueError(f"source 'wikipedia' only talks to {REAL_HOST}, not {base_url!r}")

    def canonical_url(self, url: str) -> str:
        parts = urlsplit(url.strip())
        if parts.netloc.lower() != self._parts.netloc:
            raise ValueError(f"{url!r} is not a {REAL_HOST} URL")
        if parts.path == "/robots.txt" and not parts.query:
            return urlunsplit((self._parts.scheme, self._parts.netloc, "/robots.txt", "", ""))
        if parts.query:
            raise ValueError(f"{url!r}: query strings are not article pages")
        if not parts.path.startswith("/wiki/"):
            raise ValueError(f"{url!r}: only /wiki/<Title> pages are allowed (robots.txt)")
        title = unquote(parts.path.removeprefix("/wiki/")).replace(" ", "_").strip("_")
        if not title:
            raise ValueError(f"{url!r}: no article title")
        if ":" in title and title.split(":", 1)[0].lower() in _NOT_ARTICLES:
            raise ValueError(f"{url!r}: {title.split(':', 1)[0]}: is not an article namespace")
        path = "/wiki/" + quote(title, safe=_TITLE_SAFE)
        return urlunsplit((self._parts.scheme, self._parts.netloc, path, "", ""))

    def block_reason(self, status: int, html: str) -> str | None:
        # A 403 is how Wikimedia blocks a client ("may be blocked without notice"). 429 is a
        # slow-down request and goes through the fetcher's Retry-After path instead.
        return "http_403" if status == 403 else None

    def cache_policy(self, kind: PageKind, status: int, event_date: date | None) -> CachePolicy:
        if status == 404:
            return CachePolicy(ttl=timedelta(days=1))  # the article may be created later
        match kind:
            case PageKind.ROBOTS:
                return CachePolicy(ttl=timedelta(hours=24))  # RFC 9309's maximum
            case PageKind.EVENTS_COMPLETED:  # the "{year} in UFC" list
                return CachePolicy(ttl=YEAR_LIST_TTL)
            case PageKind.EVENT:
                if event_date is None:
                    return CachePolicy(ttl=EVENT_TTL)  # unknown date: assume it can change
                settled = datetime.combine(event_date + FINAL_AFTER_EVENT, time.min, UTC)
                return CachePolicy(ttl=EVENT_TTL, final_after=settled)
        raise ValueError(f"the wikipedia source has no {kind} pages")
