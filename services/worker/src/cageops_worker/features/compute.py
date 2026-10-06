"""The feature maths: pure functions, no database, no clock.

`compute_features` is handed a fighter's PRIOR fights (already filtered to strictly before the
fight by `history.prior_fights`) and returns one row of features. It cannot see the future
because the future is never passed in. Every rule here is written up in D-027.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal

Result = Literal["win", "loss", "draw", "no_contest", "unknown"]

# Windows over a fighter's prior fights, most recent last: None means all of them.
WINDOWS: dict[str, int | None] = {"career": None, "last3": 3, "last5": 5}

# Round lengths in seconds for the formats we trust, keyed by scheduled rounds. The real
# time-format string ("3 Rnd (5-5-5)") is not stored, so anything else gets NULL duration (D-027).
STANDARD_FORMATS: dict[int, tuple[int, ...]] = {3: (300,) * 3, 5: (300,) * 5}
FULL_ROUND_SEC = 300

DURATION_NULL_REASONS = (
    "format_not_standard",
    "finish_round_out_of_range",
    "finish_time_out_of_range",
    "decision_not_full_length",
)


@dataclass(frozen=True)
class Stats:
    """One fighter's whole-fight numbers. None means the source didn't say (never 0)."""

    sig_landed: int | None
    sig_att: int | None
    td_landed: int | None
    td_att: int | None
    sub_att: int | None
    knockdowns: int | None


@dataclass(frozen=True)
class PriorFight:
    """An earlier fight from one fighter's point of view."""

    fight_id: int
    event_date: date
    result: Result
    method: str | None
    duration_sec: int | None  # None when we can't trust it (see fight_duration)
    own: Stats | None
    opp: Stats | None


@dataclass(frozen=True)
class Subject:
    """What we know about the fight being featured, and the fighter in it, before it starts."""

    event_date: date
    dob: date | None
    height_cm: float | None
    reach_cm: float | None
    stance: str | None
    weight_class: str | None
    is_title_fight: bool | None
    scheduled_rounds: int | None


def fight_duration(
    scheduled_rounds: int | None,
    finish_round: int | None,
    finish_time_sec: int | None,
    method: str | None,
) -> tuple[int | None, str | None]:
    """(seconds, None) when the length is trustworthy, else (None, reason)."""
    lengths = STANDARD_FORMATS.get(scheduled_rounds) if scheduled_rounds else None
    if lengths is None:
        return None, "format_not_standard"
    if finish_round is None or not 1 <= finish_round <= len(lengths):
        return None, "finish_round_out_of_range"
    if finish_time_sec is None or not 1 <= finish_time_sec <= lengths[finish_round - 1]:
        return None, "finish_time_out_of_range"
    if method == "decision" and (
        finish_round != len(lengths) or finish_time_sec != lengths[finish_round - 1]
    ):
        return None, "decision_not_full_length"
    return sum(lengths[: finish_round - 1]) + finish_time_sec, None


def win_streak(prior: Sequence[PriorFight]) -> int | None:
    """Walk back from the latest fight: a win extends the streak, a loss or draw ends it, a no
    contest is skipped, and an unknown result we reach makes the streak NULL."""
    streak = 0
    for fight in reversed(prior):
        if fight.result == "win":
            streak += 1
        elif fight.result in ("loss", "draw"):
            break
        elif fight.result == "unknown":
            return None
    return streak


def finish_rate(prior: Sequence[PriorFight]) -> float | None:
    """Finishing wins (KO/TKO or submission) over fights with a decided result (win or loss)."""
    decided = [f for f in prior if f.result in ("win", "loss")]
    if not decided:
        return None
    finishes = sum(1 for f in decided if f.result == "win" and f.method in ("ko_tko", "submission"))
    return finishes / len(decided)


def _ratio(
    fights: Sequence[PriorFight],
    numerator: Callable[[PriorFight], float | None],
    denominator: Callable[[PriorFight], float | None],
) -> float | None:
    """Sum of numerators over sum of denominators, using only fights where both are known.
    NULL when nothing is usable or the denominator sums to zero."""
    num = den = 0.0
    used = False
    for fight in fights:
        n, d = numerator(fight), denominator(fight)
        if n is None or d is None:
            continue
        num, den, used = num + n, den + d, True
    if not used or den == 0:
        return None
    return num / den


def _own(field: str) -> Callable[[PriorFight], float | None]:
    return lambda f: getattr(f.own, field) if f.own else None


def _opp(field: str) -> Callable[[PriorFight], float | None]:
    return lambda f: getattr(f.opp, field) if f.opp else None


def _minutes(f: PriorFight) -> float | None:
    return f.duration_sec / 60 if f.duration_sec is not None else None


def _per15(f: PriorFight) -> float | None:
    return f.duration_sec / 900 if f.duration_sec is not None else None


def _one_minus(value: float | None) -> float | None:
    return None if value is None else 1 - value


def window_features(fights: Sequence[PriorFight]) -> dict[str, float | int | None]:
    """The rates and counts for one window of prior fights (names get a window suffix later)."""
    return {
        "sig_str_landed_pm": _ratio(fights, _own("sig_landed"), _minutes),
        "sig_str_absorbed_pm": _ratio(fights, _opp("sig_landed"), _minutes),
        "sig_str_acc": _ratio(fights, _own("sig_landed"), _own("sig_att")),
        "sig_str_def": _one_minus(_ratio(fights, _opp("sig_landed"), _opp("sig_att"))),
        "td_landed_per15": _ratio(fights, _own("td_landed"), _per15),
        "td_def": _one_minus(_ratio(fights, _opp("td_landed"), _opp("td_att"))),
        "sub_att_per15": _ratio(fights, _own("sub_att"), _per15),
        "kd_per15": _ratio(fights, _own("knockdowns"), _per15),
        "n_fights": len(fights),
        "n_fights_with_stats": sum(1 for f in fights if f.own and f.opp),
        "n_fights_with_duration": sum(1 for f in fights if f.duration_sec is not None),
    }


def compute_features(
    prior: Sequence[PriorFight], subject: Subject, *, has_unresolved_prior_fight: bool = False
) -> dict[str, object]:
    """One row of features. `prior` must be sorted (event_date, fight_id), oldest first, and
    contain only fights strictly before `subject.event_date`."""
    row: dict[str, object] = {}
    for window, size in WINDOWS.items():
        chosen = prior if size is None else prior[-size:]
        for name, value in window_features(chosen).items():
            row[f"{name}_{window}"] = value
    row |= {
        "prior_ufc_fights": len(prior),
        "win_streak": win_streak(prior),
        "finish_rate": finish_rate(prior),
        "days_since_last_fight": (subject.event_date - prior[-1].event_date).days
        if prior
        else None,
        "age_days": (subject.event_date - subject.dob).days if subject.dob else None,
        "height_cm": subject.height_cm,
        "reach_cm": subject.reach_cm,
        "stance": subject.stance,
        "weight_class": subject.weight_class,
        "is_title_fight": subject.is_title_fight,
        "scheduled_rounds": subject.scheduled_rounds,
        "has_unresolved_prior_fight": has_unresolved_prior_fight,
    }
    return row
