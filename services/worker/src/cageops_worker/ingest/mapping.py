"""Turn parsed pages into database rows. Pure: no network, no database, no logging.

The parsers return what the page says ("Decision - Unanimous", "Welterweight Bout"). This module
puts that into our vocabulary with the SAME functions the seed loader uses (normalize_result,
fight_weight_class, clean_text), so a scraped row and a seeded row for the same fight go through
identical code. Rows are keyed by ufcstats ids; the store turns those into database ids.

Strict for fights that happened (an unknown method or weight class raises, like the seed loader),
lenient for fights that haven't (an odd weight class becomes NULL plus an anomaly, so one strange
string doesn't block a whole upcoming card).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cageops_scraper.parsers.models import (
    EventBout,
    EventPage,
    FighterPage,
    FightPage,
    FightStats,
)
from cageops_worker.ingest.errors import MappingError
from cageops_worker.seed.normalize import UnknownValueError, clean_text, normalize_result
from cageops_worker.seed.weight_class import fight_weight_class

# Core stats are stored per fight and per round; the strike breakdown only per round.
CORE_STATS = (
    "knockdowns",
    "sig_strikes_landed",
    "sig_strikes_att",
    "total_strikes_landed",
    "total_strikes_att",
    "takedowns_landed",
    "takedowns_att",
    "submission_att",
    "reversals",
    "ctrl_sec",
)
BREAKDOWN_STATS = tuple(
    f"{zone}_{kind}"
    for zone in ("head", "body", "leg", "distance", "clinch", "ground")
    for kind in ("landed", "att")
)
# Weight classes that don't say whether the fight was between men or women.
GENDER_UNKNOWN_CLASSES = ("Catch Weight", "Open Weight")


@dataclass(frozen=True)
class MappedFight:
    """A fight that happened, ready for the store."""

    fight: dict[str, Any]  # columns, plus ufcstats-keyed fields the store resolves to ids
    fighters: list[dict[str, str]]  # [{ufcstats_id, name}] in page order
    totals: list[dict[str, Any]]
    rounds: list[dict[str, Any]]
    anomalies: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MappedScheduledBout:
    """A bout on a card that has no result yet."""

    fight: dict[str, Any]
    fighters: list[dict[str, str]]
    anomalies: list[str] = field(default_factory=list)


# -- events and fighters ---------------------------------------------------------------------


def map_event(page: EventPage) -> dict[str, Any]:
    """The event row. event_date comes from the EVENT PAGE and nowhere else."""
    return {
        "ufcstats_id": page.ufcstats_id,
        "name": page.name,
        "event_date": page.event_date,
        "city": clean_text(page.city),
        "state": clean_text(page.state),
        "country": clean_text(page.country),
    }


def map_fighter(page: FighterPage) -> dict[str, Any]:
    """The fighter's bio columns. (The page's career rates aren't in the model, so can't leak.)"""
    return {
        "ufcstats_id": page.ufcstats_id,
        "name": page.name,
        "dob": page.dob,
        "height_cm": page.height_cm,
        "reach_cm": page.reach_cm,
        "stance": clean_text(page.stance),
    }


# -- weight class and gender -------------------------------------------------------------------


def gender_for(weight_class: str | None) -> tuple[str, bool]:
    """('M' or 'F', whether that was a guess). Only the "Women's ..." divisions say F."""
    if weight_class is not None and weight_class.startswith("Women's"):
        return "F", False
    return "M", weight_class is None or weight_class in GENDER_UNKNOWN_CLASSES


def _division_from_title(bout_title: str) -> str | None:
    """'UFC Heavyweight Title Bout' -> 'Heavyweight'; None if we don't recognise it."""
    try:
        return fight_weight_class(bout_title.removesuffix(" Bout")).canonical
    except UnknownValueError:
        return None


# -- a fight that happened ----------------------------------------------------------------------


def map_fight(page: FightPage, bout: EventBout | None = None) -> MappedFight:
    """The fight row plus its totals and per-round rows.

    `bout` is the event page's row for this fight. It supplies the plain division name
    ("Heavyweight", not "UFC Heavyweight Title Bout") and lets us cross-check the result; the
    fight page stays the authority for the result.
    """
    anomalies: list[str] = []
    by_id = {f.ufcstats_id: f for f in page.fighters}
    outcome, winner_id = _outcome(page)

    weight_class = _weight_class(page, bout, anomalies)
    gender, guessed = gender_for(weight_class)
    if guessed:
        anomalies.append(f"gender_assumed:{weight_class or 'unknown weight class'}")

    method = normalize_result(page.method_raw, page.details_raw)
    if bout is not None and (bout.outcome, bout.winner_id) != (outcome, winner_id):
        anomalies.append(f"event_fight_result_mismatch:fight={page.fight_id}")

    fight = {
        "ufcstats_id": page.fight_id,
        "event_ufcstats_id": page.event_id,
        "participants": [f.ufcstats_id for f in page.fighters],
        "red_ufcstats_id": page.fighters[0].ufcstats_id,  # first on the fight page = red corner
        "winner_ufcstats_id": winner_id,
        "status": "completed",
        "weight_class": weight_class,
        "gender": gender,
        "gender_guessed": guessed,  # the store won't let a guess overwrite a known gender
        "is_title_fight": page.is_title_fight,
        "scheduled_rounds": page.scheduled_rounds,
        "outcome": outcome,
        "method": method.method,
        "decision_type": method.decision_type,
        "method_detail": method.method_detail,
        "finish_round": page.round,
        "finish_time_sec": page.finish_time_sec,
        "referee": clean_text(page.referee),
        "has_round_stats": page.has_round_stats,
    }
    assert set(by_id) == set(fight["participants"])
    return MappedFight(
        fight=fight,
        fighters=[{"ufcstats_id": f.ufcstats_id, "name": f.name} for f in page.fighters],
        totals=[_stats_row(s, CORE_STATS) for s in page.totals],
        rounds=[_stats_row(s, CORE_STATS + BREAKDOWN_STATS) for s in page.rounds],
        anomalies=anomalies + list(page.anomalies),
    )


def _outcome(page: FightPage) -> tuple[str, str | None]:
    """(outcome, winner ufcstats id) from the two result badges."""
    badges = sorted(f.result for f in page.fighters)
    if badges == ["L", "W"]:
        winner = next(f.ufcstats_id for f in page.fighters if f.result == "W")
        return "win", winner
    if badges == ["D", "D"]:
        return "draw", None
    if badges == ["NC", "NC"]:
        return "no_contest", None
    raise MappingError(f"fight {page.fight_id}: result badges {badges} don't describe one result")


def _weight_class(page: FightPage, bout: EventBout | None, anomalies: list[str]) -> str | None:
    from_title = _division_from_title(page.bout_title_raw)
    if bout is None or bout.weight_class_raw is None:
        if from_title is None:  # no plain name to fall back on: unknown values fail, as in the seed
            return fight_weight_class(page.bout_title_raw.removesuffix(" Bout")).canonical
        return from_title
    plain = fight_weight_class(bout.weight_class_raw).canonical  # raises if unknown
    if from_title is not None and from_title != plain:
        anomalies.append(f"weight_class_mismatch:{plain}!={from_title}")
    return plain


def _stats_row(stats: FightStats, columns: tuple[str, ...]) -> dict[str, Any]:
    row: dict[str, Any] = {"fighter_ufcstats_id": stats.fighter_id}
    if stats.round is not None:
        row["round"] = stats.round
    row.update({column: getattr(stats, column) for column in columns})
    return row


# -- a bout that hasn't happened ------------------------------------------------------------------


def split_bouts(event: EventPage) -> tuple[list[EventBout], list[EventBout]]:
    """(bouts with a result, bouts without). Only the first need a fight page fetched."""
    played = [b for b in event.bouts if b.outcome is not None]
    unplayed = [b for b in event.bouts if b.outcome is None]
    return played, unplayed


def map_scheduled_bout(bout: EventBout, event_ufcstats_id: str) -> MappedScheduledBout:
    """A scheduled bout. Everything about its result is NULL, and the title flag and round count
    are unknown (the event page doesn't say), so they stay NULL rather than a made-up false."""
    anomalies: list[str] = []
    weight_class = None
    if bout.weight_class_raw is not None:
        try:
            weight_class = fight_weight_class(bout.weight_class_raw).canonical
        except UnknownValueError:
            anomalies.append(f"unknown_weight_class:{bout.weight_class_raw}")
    gender, guessed = gender_for(weight_class)
    if guessed:
        anomalies.append(f"gender_assumed:{weight_class or 'unknown weight class'}")
    fight = {
        "ufcstats_id": bout.fight_id,
        "event_ufcstats_id": event_ufcstats_id,
        "participants": [f.ufcstats_id for f in bout.fighters],
        "red_ufcstats_id": None,  # the card order isn't verified as corner order
        "winner_ufcstats_id": None,
        "status": "scheduled",
        "weight_class": weight_class,
        "gender": gender,
        "gender_guessed": guessed,
        "is_title_fight": None,
        "scheduled_rounds": None,
        "outcome": None,
        "method": None,
        "decision_type": None,
        "method_detail": None,
        "finish_round": None,
        "finish_time_sec": None,
        "referee": None,
        "has_round_stats": False,
    }
    return MappedScheduledBout(
        fight=fight,
        fighters=[{"ufcstats_id": f.ufcstats_id, "name": f.name} for f in bout.fighters],
        anomalies=anomalies,
    )
