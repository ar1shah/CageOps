"""Database writes for ingestion. One function per kind of write; each takes an open connection
so a job can do all of its writes in ONE transaction (all or nothing).

Rows arrive keyed by ufcstats ids (see mapping.py); this module turns them into database ids,
puts a fight's two fighters in neutral order (fighter_a_id < fighter_b_id, D-009), and applies the
column policies in upsert.py.

Who may write what:
- events.event_date is written only by write_event, from the EVENT page.
- A completed fight is written only by write_completed_fight, from its fight page.
- A scheduled bout may never overwrite a completed fight.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection

from cageops_common.db.models import Event, Fight, Fighter, FightRoundStats, FightTotals
from cageops_worker.ingest.errors import MissingPrerequisite
from cageops_worker.ingest.mapping import (
    BREAKDOWN_STATS,
    CORE_STATS,
    MappedFight,
    MappedScheduledBout,
)
from cageops_worker.ingest.upsert import UpsertCounts, insert_missing, upsert

FIGHTS = Fight.__table__
RESULT_COLUMNS = (
    "outcome",
    "winner_id",
    "method",
    "decision_type",
    "method_detail",
    "finish_round",
    "finish_time_sec",
)


# -- events and fighters ---------------------------------------------------------------------


def write_event(conn: Connection, row: dict[str, Any]) -> tuple[UpsertCounts, int]:
    """Upsert an event from its event page. Returns (counts, the event's database id)."""
    counts = upsert(
        conn,
        Event.__table__,
        [row],
        key=["ufcstats_id"],
        replace=["name", "event_date"],  # the event page is the authority on both
        fill=["city", "state", "country"],
    )
    return counts, event_id(conn, row["ufcstats_id"])  # type: ignore[return-value]


def event_id(conn: Connection, ufcstats_id: str) -> int | None:
    return conn.execute(
        sa.select(Event.__table__.c.id).where(Event.__table__.c.ufcstats_id == ufcstats_id)
    ).scalar()


def fighter_ids(conn: Connection, ufcstats_ids: Iterable[str]) -> dict[str, int]:
    ids = list(ufcstats_ids)
    rows = conn.execute(
        sa.select(Fighter.__table__.c.ufcstats_id, Fighter.__table__.c.id).where(
            Fighter.__table__.c.ufcstats_id.in_(ids)
        )
    )
    return {ufcstats_id: db_id for ufcstats_id, db_id in rows}


def ensure_fighters(
    conn: Connection, fighters: Sequence[dict[str, str]]
) -> tuple[UpsertCounts, dict[str, int]]:
    """Make sure a row exists for each fighter (a name now; the fighter job fills in the bio).
    Existing rows are left exactly as they are."""
    counts = insert_missing(
        conn,
        Fighter.__table__,
        [{"ufcstats_id": f["ufcstats_id"], "name": f["name"]} for f in fighters],
        key=["ufcstats_id"],
    )
    return counts, fighter_ids(conn, [f["ufcstats_id"] for f in fighters])


def write_fighter_bio(conn: Connection, row: dict[str, Any]) -> UpsertCounts:
    """Upsert a fighter's bio from their page. A blank on the page never erases a value we hold."""
    return upsert(
        conn,
        Fighter.__table__,
        [row],
        key=["ufcstats_id"],
        replace=["name"],
        fill=["dob", "height_cm", "reach_cm", "stance"],
    )


# -- fights ----------------------------------------------------------------------------------------


def _neutral_order(ids: Sequence[int]) -> tuple[int, int]:
    a, b = sorted(ids)
    return a, b


def _fight_row(fight: dict[str, Any], event_db_id: int, ids: dict[str, int]) -> dict[str, Any]:
    a, b = _neutral_order([ids[u] for u in fight["participants"]])
    red = fight["red_ufcstats_id"]
    winner = fight["winner_ufcstats_id"]
    row = {
        k: v
        for k, v in fight.items()
        if k not in ("participants", "red_ufcstats_id", "winner_ufcstats_id", "gender_guessed")
        and k != "event_ufcstats_id"
    }
    row |= {
        "event_id": event_db_id,
        "fighter_a_id": a,
        "fighter_b_id": b,
        "red_fighter_id": ids[red] if red else None,
        "winner_id": ids[winner] if winner else None,
    }
    return row


def write_scheduled_bouts(
    conn: Connection, event_ufcstats_id: str, bouts: Sequence[MappedScheduledBout]
) -> UpsertCounts:
    """Upsert the bouts on a card that have no result yet. Never touches a completed fight."""
    if not bouts:
        return UpsertCounts()
    event_db_id = event_id(conn, event_ufcstats_id)
    if event_db_id is None:
        raise MissingPrerequisite(f"event {event_ufcstats_id} is not in the database yet")
    _, ids = ensure_fighters(conn, [f for b in bouts for f in b.fighters])
    rows = [_fight_row(b.fight, event_db_id, ids) for b in bouts]
    # Only what a scheduled bout knows. Its result columns stay NULL (not listed, so never set),
    # and a cancelled bout that reappears becomes scheduled again.
    return upsert(
        conn,
        FIGHTS,
        rows,
        key=["ufcstats_id"],
        replace=["event_id", "fighter_a_id", "fighter_b_id", "weight_class", "gender", "status"],
        sticky_true=["has_round_stats"],
        only_if=FIGHTS.c.status != "completed",
    )


@dataclass
class ReconcileResult:
    cancelled: list[str] = field(default_factory=list)  # fight ufcstats ids
    anomalies: list[str] = field(default_factory=list)


def reconcile_event_bouts(
    conn: Connection, event_db_id: int, present_fight_ids: Sequence[str]
) -> ReconcileResult:
    """A bout we hold as scheduled but the event page no longer lists has been taken off the card:
    mark it cancelled (never delete it, so a stored prediction stays explainable).

    Guard: a page that lists NO bouts while we hold scheduled ones is more likely a bad page than
    a card where every fight was cancelled, so cancel nothing and say so.
    """
    result = ReconcileResult()
    held = conn.execute(
        sa.select(FIGHTS.c.ufcstats_id, FIGHTS.c.status).where(FIGHTS.c.event_id == event_db_id)
    ).all()
    present = set(present_fight_ids)
    scheduled_missing = [u for u, status in held if status == "scheduled" and u not in present]
    if not present:
        if scheduled_missing:
            result.anomalies.append(f"event_page_has_no_bouts:event_id={event_db_id}")
        return result
    if scheduled_missing:
        conn.execute(
            sa.update(FIGHTS)
            .where(FIGHTS.c.ufcstats_id.in_(scheduled_missing), FIGHTS.c.status == "scheduled")
            .values(status="cancelled")
        )
        result.cancelled = scheduled_missing
    result.anomalies += [
        f"completed_fight_missing_from_event:fight={u}"
        for u, status in held
        if status == "completed" and u not in present
    ]
    return result


def cancel_scheduled_for_event(conn: Connection, event_db_id: int) -> list[str]:
    """The event page is gone (404): every bout still scheduled for it is cancelled."""
    rows = conn.execute(
        sa.update(FIGHTS)
        .where(FIGHTS.c.event_id == event_db_id, FIGHTS.c.status == "scheduled")
        .values(status="cancelled")
        .returning(FIGHTS.c.ufcstats_id)
    )
    return [r[0] for r in rows]


@dataclass
class FightWriteResult:
    fight: UpsertCounts
    totals: UpsertCounts
    rounds: UpsertCounts
    stale_stat_rows_deleted: int = 0
    previous_status: str | None = None  # None if the fight was new
    anomalies: list[str] = field(default_factory=list)


def write_completed_fight(conn: Connection, mapped: MappedFight) -> FightWriteResult:
    """Upsert a fight that happened, with its totals and per-round stats.

    The event row must already exist: it is where the fight's date lives, and we never guess one.
    """
    fight = mapped.fight
    event_db_id = event_id(conn, fight["event_ufcstats_id"])
    if event_db_id is None:
        raise MissingPrerequisite(
            f"event {fight['event_ufcstats_id']} is not in the database yet; "
            f"fight {fight['ufcstats_id']} takes its date from the event page"
        )
    _, ids = ensure_fighters(conn, mapped.fighters)
    row = _fight_row(fight, event_db_id, ids)
    previous = conn.execute(
        sa.select(FIGHTS.c.status, FIGHTS.c.gender, *(FIGHTS.c[c] for c in RESULT_COLUMNS)).where(
            FIGHTS.c.ufcstats_id == fight["ufcstats_id"]
        )
    ).one_or_none()
    if fight["gender_guessed"] and previous is not None:
        # A guess must not overwrite a gender we already know. (Not None: Postgres checks NOT NULL
        # on the proposed row before it looks for a conflict, so None would fail even though the
        # row exists. Passing the current value makes the update a no-op.)
        row["gender"] = previous.gender

    anomalies: list[str] = []
    if previous is not None and previous.status == "completed":
        changed = [c for c in RESULT_COLUMNS if previous._mapping[c] != row[c]]
        if changed:
            diff = ",".join(f"{c}={previous._mapping[c]}->{row[c]}" for c in changed)
            anomalies.append(f"result_changed:fight={fight['ufcstats_id']}:{diff}")

    counts = upsert(
        conn,
        FIGHTS,
        [row],
        key=["ufcstats_id"],
        # the result is replaced as a unit, NULLs included: an overturned win must clear its winner
        replace=[
            *RESULT_COLUMNS,
            "status",
            "event_id",
            "fighter_a_id",
            "fighter_b_id",
            "red_fighter_id",
            "is_title_fight",
        ],
        fill=["weight_class", "gender", "scheduled_rounds", "referee"],
        sticky_true=["has_round_stats"],
    )
    fight_db_id = conn.execute(
        sa.select(FIGHTS.c.id).where(FIGHTS.c.ufcstats_id == fight["ufcstats_id"])
    ).scalar_one()

    totals, rounds, deleted = _write_stats(conn, fight_db_id, ids, mapped)
    return FightWriteResult(
        fight=counts,
        totals=totals,
        rounds=rounds,
        stale_stat_rows_deleted=deleted,
        previous_status=None if previous is None else previous.status,
        anomalies=anomalies,
    )


def _write_stats(
    conn: Connection, fight_db_id: int, ids: dict[str, int], mapped: MappedFight
) -> tuple[UpsertCounts, UpsertCounts, int]:
    total_rows = [
        {"fight_id": fight_db_id, "fighter_id": ids[t["fighter_ufcstats_id"]]}
        | {c: t[c] for c in CORE_STATS}
        for t in mapped.totals
    ]
    round_rows = [
        {"fight_id": fight_db_id, "fighter_id": ids[r["fighter_ufcstats_id"]], "round": r["round"]}
        | {c: r[c] for c in CORE_STATS + BREAKDOWN_STATS}
        for r in mapped.rounds
    ]
    totals = upsert(
        conn,
        FightTotals.__table__,
        total_rows,
        key=["fight_id", "fighter_id"],
        fill=list(CORE_STATS),
    )
    rounds = upsert(
        conn,
        FightRoundStats.__table__,
        round_rows,
        key=["fight_id", "fighter_id", "round"],
        fill=list(CORE_STATS + BREAKDOWN_STATS),
    )
    # The page is the full picture: a round that is no longer on it is removed. (Only when the
    # page has stats at all; a page with none never deletes what we hold.)
    deleted = 0
    if round_rows:
        keep = [(r["fighter_id"], r["round"]) for r in round_rows]
        deleted += _delete_missing(
            conn, FightRoundStats.__table__, fight_db_id, keep, ("fighter_id", "round")
        )
    if total_rows:
        keep = [(t["fighter_id"],) for t in total_rows]
        deleted += _delete_missing(conn, FightTotals.__table__, fight_db_id, keep, ("fighter_id",))
    return totals, rounds, deleted


def _delete_missing(
    conn: Connection,
    table: sa.Table,
    fight_db_id: int,
    keep: Sequence[tuple],
    key_columns: Sequence[str],
) -> int:
    cols = sa.tuple_(*(table.c[c] for c in key_columns))
    result = conn.execute(
        sa.delete(table).where(table.c.fight_id == fight_db_id, cols.not_in(keep))
    )
    return result.rowcount
