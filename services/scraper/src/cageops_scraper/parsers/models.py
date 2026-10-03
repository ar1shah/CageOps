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


class FightFighter(Parsed):
    ufcstats_id: str
    name: str
    # W win, L loss, D draw, NC no contest. Straight from the page's status badge.
    result: Literal["W", "L", "D", "NC"]


class FightStats(Parsed):
    """One fighter's numbers for one scope: the whole fight (round is None) or a single round.

    None means the page showed no value ("--"); 0 means the page published 0. The strike
    breakdown (head ... ground) comes from a second table, so it can be None while the core
    numbers are present.
    """

    fighter_id: str
    round: int | None
    knockdowns: int | None = None
    sig_strikes_landed: int | None = None
    sig_strikes_att: int | None = None
    total_strikes_landed: int | None = None
    total_strikes_att: int | None = None
    takedowns_landed: int | None = None
    takedowns_att: int | None = None
    submission_att: int | None = None
    reversals: int | None = None
    ctrl_sec: int | None = None
    head_landed: int | None = None
    head_att: int | None = None
    body_landed: int | None = None
    body_att: int | None = None
    leg_landed: int | None = None
    leg_att: int | None = None
    distance_landed: int | None = None
    distance_att: int | None = None
    clinch_landed: int | None = None
    clinch_att: int | None = None
    ground_landed: int | None = None
    ground_att: int | None = None


class FightPage(Parsed):
    """A fight page. There is deliberately NO date field: fight pages don't carry one, and the
    fight's date must come from its event page (point-in-time correctness).

    `fighters` is in the page's order, which for the fight page is the card's order: the FIRST
    fighter is the red corner (checked against the seed's red corner on every fixture).
    """

    fight_id: str
    event_id: str
    fighters: tuple[FightFighter, FightFighter]
    bout_title_raw: str  # as printed: "Welterweight Bout", "UFC Heavyweight Title Bout"
    is_title_fight: bool
    method_raw: str  # "Decision - Unanimous", "KO/TKO", "Submission", "Could Not Continue", ...
    details_raw: str | None  # judges' scores, the strike or submission name, ...
    round: int | None
    finish_time_sec: int | None  # seconds into the finishing round
    time_format_raw: str | None  # "3 Rnd (5-5-5)"
    scheduled_rounds: int | None
    referee: str | None
    has_round_stats: bool  # False when the page says round-by-round stats aren't available
    totals: list[FightStats]  # whole-fight rows, one per fighter; empty when no stats
    rounds: list[FightStats]  # per-round rows, one per fighter per round; empty when no stats
    # Suspicious values that don't stop parsing. Each is "kind:detail"; the ingestion job logs
    # them and counts them (see anomalies.py).
    anomalies: list[str] = []
