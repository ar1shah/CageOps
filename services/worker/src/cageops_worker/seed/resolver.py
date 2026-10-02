"""Resolve fighter names from other sources (odds, rankings) to fighter ids.

Rules (D-010):
- Match on the normalized name (accents stripped, punctuation dropped).
- If a name maps to more than one fighter, NEVER pick one. Narrow by when the fighter was
  active and the weight class, and if that doesn't leave exactly one, report it as ambiguous.
- Anything left over is unmatched. Callers keep those rows with a NULL fighter id and list
  them in the load report; nothing is dropped silently.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import Connection, text

from cageops_worker.seed.normalize import normalize_name

ALIASES_CSV = Path(__file__).with_name("aliases.csv")
ACTIVE_MARGIN = timedelta(days=60)


@dataclass(frozen=True)
class Resolution:
    fighter_id: int | None
    status: str  # matched | alias | disambiguated | ambiguous | unmatched


class NameResolver:
    def __init__(
        self,
        fighters: dict[int, str],
        aliases: dict[str, int] | None = None,
        activity: dict[int, list[tuple[date, str | None]]] | None = None,
    ):
        self._by_name: dict[str, list[int]] = defaultdict(list)
        for fighter_id, name in fighters.items():
            self._by_name[normalize_name(name)].append(fighter_id)
        self._aliases = aliases or {}
        self._activity = activity or {}

    def resolve(
        self, name: str, *, weight_class: str | None = None, on: date | None = None
    ) -> Resolution:
        key = normalize_name(name)
        candidates = self._by_name.get(key, [])
        if len(candidates) == 1:
            return Resolution(candidates[0], "matched")
        if not candidates:
            if key in self._aliases:
                return Resolution(self._aliases[key], "alias")
            return Resolution(None, "unmatched")
        narrowed = self._narrow(candidates, weight_class, on)
        if len(narrowed) == 1:
            return Resolution(narrowed[0], "disambiguated")
        return Resolution(None, "ambiguous")

    def _narrow(
        self, candidates: list[int], weight_class: str | None, on: date | None
    ) -> list[int]:
        remaining = candidates
        if on is not None:
            remaining = [c for c in remaining if self._active_on(c, on)]
        if weight_class is not None:
            remaining = [
                c
                for c in remaining
                if any(wc == weight_class for _, wc in self._activity.get(c, []))
            ]
        return remaining

    def _active_on(self, fighter_id: int, on: date) -> bool:
        dates = [d for d, _ in self._activity.get(fighter_id, [])]
        return bool(dates) and min(dates) - ACTIVE_MARGIN <= on <= max(dates) + ACTIVE_MARGIN

    @classmethod
    def from_db(cls, conn: Connection, aliases_csv: Path | None = ALIASES_CSV) -> NameResolver:
        fighters = dict(conn.execute(text("SELECT id, name FROM fighters")).all())
        aliases = {
            normalize_name(row[0]): row[1]
            for row in conn.execute(text("SELECT alias_norm, fighter_id FROM fighter_aliases"))
        }
        activity: dict[int, list[tuple[date, str | None]]] = defaultdict(list)
        for fighter_id, event_date, weight_class in conn.execute(
            text(
                "SELECT p.fid, e.event_date, f.weight_class FROM fights f"
                " JOIN events e ON e.id = f.event_id"
                " CROSS JOIN LATERAL (VALUES (f.fighter_a_id), (f.fighter_b_id)) AS p(fid)"
            )
        ):
            activity[fighter_id].append((event_date, weight_class))
        return cls(fighters, aliases, activity)


def load_aliases(conn: Connection, csv_path: Path = ALIASES_CSV) -> int:
    """Upsert the reviewed alias list (alias, ufcstats_id) into fighter_aliases."""
    if not csv_path.exists():
        return 0
    with csv_path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    ids = dict(conn.execute(text("SELECT ufcstats_id, id FROM fighters")).all())
    loaded = 0
    for row in rows:
        fighter_id = ids.get(row["ufcstats_id"])
        if fighter_id is None:
            raise ValueError(
                f"alias {row['alias']!r} points at unknown fighter {row['ufcstats_id']}"
            )
        conn.execute(
            text(
                "INSERT INTO fighter_aliases (alias_norm, fighter_id, source)"
                " VALUES (:alias, :fid, 'reviewed_csv')"
                " ON CONFLICT (alias_norm) DO UPDATE SET fighter_id = excluded.fighter_id"
            ),
            {"alias": normalize_name(row["alias"]), "fid": fighter_id},
        )
        loaded += 1
    return loaded
