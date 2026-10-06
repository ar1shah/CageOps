"""What an English Wikipedia UFC page says, typed. Frozen, no unknown fields (like models.py).

These hold the page's own words ("TKO (punches)", "def."). Mapping them onto our database
vocabulary happens in the worker. Prose is deliberately absent: the notes that mark a title fight
are read once, reduced to a `note_kind`, and the text itself is never kept (D-029).
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from cageops_scraper.parsers.models import Parsed

# What a bout's footnote says about the belt: a UFC championship (undisputed, interim or vacant),
# the BMF belt (which the seed does not count as a title fight), or nothing about a title.
NoteKind = Literal["championship", "bmf"] | None


class WikiEventRef(Parsed):
    """One row of a "{year} in UFC" past-events table."""

    title: str | None  # the article's /wiki/<Title>; None if the event has no article
    name: str
    event_date: date


class WikiFighter(Parsed):
    name: str  # as shown, with (c) and footnote markers removed
    # The /wiki/ link target (or the title of a red link): stable when the display name is edited.
    link_title: str | None
    champion_marker: bool  # shown as "(c)" or "(ic)" next to the name


class WikiBout(Parsed):
    weight_class_raw: str
    first: WikiFighter  # the left column: the winner when `versus` is "def."
    second: WikiFighter
    versus: Literal["def.", "vs."]  # "vs." for a draw or no contest
    method_raw: str  # "Decision (unanimous) (30-27, 30-27, 30-27)", "KO (punches)", "NC (...)"
    round: int | None
    time_raw: str | None
    note_kind: NoteKind


class WikiEventPage(Parsed):
    name: str  # the infobox title, e.g. "UFC 332: Silva vs. Wang"
    event_date: date
    article_id: int  # wgArticleId: survives a rename
    revision_id: int  # wgRevisionId: which revision these results were read from
    canonical_title: str  # the article's real title (a redirect resolves to it)
    bouts: tuple[WikiBout, ...]
    anomalies: tuple[str, ...] = ()
