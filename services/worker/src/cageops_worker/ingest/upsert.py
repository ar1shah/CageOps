"""Idempotent writes that say what they did.

An upsert is "insert this row, or if one with the same key exists, update it": a sticky note
edited in place instead of a new note every time. Run the same job twice and the table ends up
identical (D-019).

Each non-key column has one of three policies:
- replace: the new value always wins, NULL included. For a fight's result, where NULL is a real
  answer (a no contest has no winner) and an overturned result must be able to clear the old one.
- fill: a new non-NULL value wins; a new NULL never erases what we already hold. Data only gets
  filled in or corrected, never blanked.
- sticky_true: a boolean that can go from false to true but never back.

Every statement also says "only write if something would actually change". So an identical rerun
writes nothing at all, and Postgres tells us for each row whether it was inserted (xmax = 0 is how
Postgres marks a row version created by this statement's INSERT rather than an UPDATE),
updated, or left alone.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from redis import Redis
from sqlalchemy import Connection, Table
from sqlalchemy.dialects.postgresql import insert

BATCH = 1000


@dataclass
class UpsertCounts:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0

    def __add__(self, other: UpsertCounts) -> UpsertCounts:
        return UpsertCounts(
            self.inserted + other.inserted,
            self.updated + other.updated,
            self.unchanged + other.unchanged,
        )

    @property
    def total(self) -> int:
        return self.inserted + self.updated + self.unchanged

    def as_dict(self) -> dict[str, int]:
        return {"inserted": self.inserted, "updated": self.updated, "unchanged": self.unchanged}


def upsert(
    conn: Connection,
    table: Table,
    rows: Sequence[dict[str, Any]],
    key: Sequence[str],
    *,
    replace: Sequence[str] = (),
    fill: Sequence[str] = (),
    sticky_true: Sequence[str] = (),
    only_if: sa.ColumnElement[bool] | None = None,
) -> UpsertCounts:
    """INSERT ... ON CONFLICT (key) DO UPDATE, per the column policies above.

    only_if is an extra condition on the EXISTING row for an update to happen (for example
    "never overwrite a completed fight with a scheduled one"); a conflicting row that fails it is
    counted as unchanged.
    """
    counts = UpsertCounts()
    if not rows:
        return counts
    keys = [tuple(row[k] for k in key) for row in rows]
    if len(set(keys)) != len(keys):
        raise ValueError(f"duplicate keys in one upsert of {table.name}: Postgres can't apply both")

    columns = [*key, *replace, *fill, *sticky_true]
    uniform = [{c: row.get(c) for c in columns} for row in rows]
    for start in range(0, len(uniform), BATCH):
        batch = uniform[start : start + BATCH]
        stmt = insert(table)
        excluded = stmt.excluded
        merged: dict[str, sa.ColumnElement] = {}
        for name in replace:
            merged[name] = excluded[name]
        for name in fill:
            merged[name] = sa.func.coalesce(excluded[name], table.c[name])
        for name in sticky_true:
            merged[name] = sa.or_(
                sa.func.coalesce(excluded[name], False), sa.func.coalesce(table.c[name], False)
            )
        if merged:
            existing = sa.tuple_(*(table.c[name] for name in merged))
            changed = existing.is_distinct_from(sa.tuple_(*merged.values()))
            where = changed if only_if is None else sa.and_(only_if, changed)
            stmt = stmt.on_conflict_do_update(index_elements=list(key), set_=merged, where=where)
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=list(key))
        stmt = stmt.returning(sa.literal_column("(xmax = 0)"))
        was_inserted = list(conn.execute(stmt, batch).scalars())
        inserted = sum(1 for flag in was_inserted if flag)
        counts.inserted += inserted
        counts.updated += len(was_inserted) - inserted
        counts.unchanged += len(batch) - len(was_inserted)
    return counts


def insert_missing(
    conn: Connection, table: Table, rows: Sequence[dict[str, Any]], key: Sequence[str]
) -> UpsertCounts:
    """Insert rows that don't exist yet and leave existing ones exactly as they are (used for
    fighter stubs: a name now, the bio filled in later by the fighter job)."""
    counts = UpsertCounts()
    deduped = {tuple(row[k] for k in key): row for row in rows}.values()
    if not deduped:
        return counts
    stmt = insert(table).on_conflict_do_nothing(index_elements=list(key))
    returned = list(conn.execute(stmt.returning(*(table.c[k] for k in key)), list(deduped)))
    counts.inserted = len(returned)
    counts.unchanged = len(deduped) - len(returned)
    return counts


# -- per-run totals, shared by every worker process --------------------------------------------


def record_counts(
    redis: Redis, run_id: str, table: str, counts: UpsertCounts, ttl_s: int = 7 * 24 * 3600
) -> None:
    """Add a job's row counts to the run's totals. HINCRBY is atomic, so several worker processes
    can report into one hash without losing updates."""
    key = f"ingest:run:{run_id}"
    with redis.pipeline() as pipe:
        for action, n in counts.as_dict().items():
            if n:
                pipe.hincrby(key, f"{table}:{action}", n)
        pipe.expire(key, ttl_s)
        pipe.execute()


def read_counts(redis: Redis, run_id: str) -> dict[str, int]:
    """{'fights:inserted': 12, ...} for one run."""
    raw = redis.hgetall(f"ingest:run:{run_id}")
    return {k.decode() if isinstance(k, bytes) else k: int(v) for k, v in raw.items()}
