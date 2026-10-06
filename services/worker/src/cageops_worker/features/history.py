"""Load fights and stats from Postgres, and answer "what did this fighter do BEFORE this date?".

`prior_fights` is the one place the strictly-before rule lives. Nothing else in the feature code
looks at dates to decide what a fighter's history is.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import Connection, text

from cageops_worker.features.compute import PriorFight, Result, Stats, fight_duration

_FIGHT_COLUMNS = """
    f.id AS fight_id, e.event_date, f.fighter_a_id, f.fighter_b_id, f.winner_id, f.outcome,
    f.method, f.scheduled_rounds, f.finish_round, f.finish_time_sec, f.weight_class,
    f.is_title_fight
"""
# `completed_fights` is the one door for finished fights (D-023); upcoming ones come from `fights`.
_COMPLETED = f"SELECT {_FIGHT_COLUMNS} FROM completed_fights f JOIN events e ON e.id = f.event_id"
_SCHEDULED = (
    f"SELECT {_FIGHT_COLUMNS} FROM fights f JOIN events e ON e.id = f.event_id"
    " WHERE f.status = 'scheduled'"
)


@dataclass(frozen=True)
class FightRecord:
    fight_id: int
    event_date: date
    fighter_a_id: int
    fighter_b_id: int
    winner_id: int | None
    outcome: str | None
    method: str | None
    scheduled_rounds: int | None
    finish_round: int | None
    finish_time_sec: int | None
    weight_class: str | None
    is_title_fight: bool | None
    duration_sec: int | None
    duration_null_reason: str | None

    @property
    def fighter_ids(self) -> tuple[int, int]:
        return self.fighter_a_id, self.fighter_b_id

    @property
    def sort_key(self) -> tuple[date, int]:
        # Within one date the order is arbitrary but stable: the fight id (D-027).
        return self.event_date, self.fight_id


@dataclass(frozen=True)
class Bio:
    dob: date | None
    height_cm: float | None
    reach_cm: float | None
    stance: str | None


@dataclass
class History:
    completed: list[FightRecord]
    scheduled: list[FightRecord]
    bios: dict[int, Bio]
    totals: dict[tuple[int, int], Stats]
    completed_by_fighter: dict[int, list[FightRecord]] = field(default_factory=dict)
    scheduled_by_fighter: dict[int, list[FightRecord]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for source, index in (
            (self.completed, self.completed_by_fighter),
            (self.scheduled, self.scheduled_by_fighter),
        ):
            grouped: dict[int, list[FightRecord]] = defaultdict(list)
            for fight in sorted(source, key=lambda f: f.sort_key):
                for fighter_id in fight.fighter_ids:
                    grouped[fighter_id].append(fight)
            index.update(grouped)


def _record(row) -> FightRecord:
    duration, reason = fight_duration(
        row["scheduled_rounds"], row["finish_round"], row["finish_time_sec"], row["method"]
    )
    return FightRecord(**dict(row), duration_sec=duration, duration_null_reason=reason)


def load_history(conn: Connection) -> History:
    completed = [_record(r) for r in conn.execute(text(_COMPLETED)).mappings()]
    # A scheduled bout has no result, so its duration is meaningless; build the record anyway.
    scheduled = [_record(r) for r in conn.execute(text(_SCHEDULED)).mappings()]
    bios = {
        r["id"]: Bio(r["dob"], r["height_cm"], r["reach_cm"], r["stance"])
        for r in conn.execute(
            text("SELECT id, dob, height_cm, reach_cm, stance FROM fighters")
        ).mappings()
    }
    totals = {
        (r["fight_id"], r["fighter_id"]): Stats(
            sig_landed=r["sig_strikes_landed"],
            sig_att=r["sig_strikes_att"],
            td_landed=r["takedowns_landed"],
            td_att=r["takedowns_att"],
            sub_att=r["submission_att"],
            knockdowns=r["knockdowns"],
        )
        for r in conn.execute(
            text(
                "SELECT fight_id, fighter_id, sig_strikes_landed, sig_strikes_att,"
                " takedowns_landed, takedowns_att, submission_att, knockdowns FROM fight_totals"
            )
        ).mappings()
    }
    return History(completed, scheduled, bios, totals)


def _result(fight: FightRecord, fighter_id: int) -> Result:
    if fight.outcome == "win":
        return "win" if fight.winner_id == fighter_id else "loss"
    if fight.outcome in ("draw", "no_contest"):
        return fight.outcome
    return "unknown"


def prior_fights(history: History, fighter_id: int, before: date) -> list[PriorFight]:
    """This fighter's completed fights with event_date STRICTLY before `before`, oldest first
    (event_date, then fight_id). A fight on the same date is not "before"."""
    prior = []
    for fight in history.completed_by_fighter.get(fighter_id, []):
        if fight.event_date >= before:
            continue
        opponent_id = fight.fighter_a_id if fight.fighter_b_id == fighter_id else fight.fighter_b_id
        prior.append(
            PriorFight(
                fight_id=fight.fight_id,
                event_date=fight.event_date,
                result=_result(fight, fighter_id),
                method=fight.method,
                duration_sec=fight.duration_sec,
                own=history.totals.get((fight.fight_id, fighter_id)),
                opp=history.totals.get((fight.fight_id, opponent_id)),
            )
        )
    return prior


def has_unresolved_prior_fight(
    history: History, fighter_id: int, before: date, today: date
) -> bool:
    """True if the fighter has a bout that already happened (dated before both `before` and
    `today`) but never got a result: a hole in their history we can't fill."""
    limit = min(before, today)
    return any(f.event_date < limit for f in history.scheduled_by_fighter.get(fighter_id, []))
