"""The typed forms the parsers fill in. Each is frozen (can't change after parsing) and rejects
unknown fields, so a typo in a parser is an error instead of a silently dropped value.

These hold what the PAGE says, in the site's own words ("Decision - Unanimous", "U-DEC"). Turning
that into our database vocabulary happens later, with the same functions the seed loader uses.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict


class Parsed(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EventListRow(Parsed):
    """One row of the events list (completed or upcoming)."""

    ufcstats_id: str
    url: str
    name: str
    event_date: date
    location_raw: str | None
    # The completed list's first row is the NEXT upcoming event, marked with a "next" icon.
    # It is not completed; callers must not treat the list it came from as proof of that.
    is_next_marker: bool


class FighterRef(Parsed):
    ufcstats_id: str
    name: str


class EventBout(Parsed):
    """One bout row on an event page.

    `fighters` is in PAGE order, and on event pages the page puts the WINNER FIRST. It is not the
    corner order and carries the result, so it must never become a database column order. The
    red corner comes only from the fight page (first fighter there).

    `winner_id` follows that winner-first order and is a convenience only: the fight page's W/L
    status is the authority, and the ingestion job compares the two.
    """

    fight_id: str
    fight_url: str
    fighters: tuple[FighterRef, FighterRef]
    outcome: Literal["win", "draw", "no_contest"] | None  # None: no result yet (upcoming)
    winner_id: str | None
    weight_class_raw: str | None
    method_raw: str | None  # abbreviated here: "U-DEC", "S-DEC", "M-DEC", "KO/TKO", "SUB"
    method_detail_raw: str | None
    round: int | None
    time_sec: int | None  # seconds into the finishing round


class EventPage(Parsed):
    """An event page. The date here is the ONLY source of a fight's date (fight pages have none).
    Works for both completed and upcoming events; upcoming bouts have no outcome."""

    ufcstats_id: str
    name: str
    event_date: date
    location_raw: str | None
    city: str | None
    state: str | None
    country: str | None
    bouts: list[EventBout]
