"""A tiny factory for building fight histories in the test database, and a way to rebuild and read
the feature table back. Fixed dates only: nothing here reads the real clock."""

from __future__ import annotations

from datetime import date
from itertools import count

import pytest
from sqlalchemy import text

from cageops_worker.features.build import FEATURE_COLUMNS, rebuild

TODAY = date(2030, 1, 1)  # far after every fight a test creates, unless a test says otherwise


class World:
    def __init__(self, engine):
        self.engine = engine
        self._ids = count(1)
        self.bios: dict[int, dict] = {}

    def fighter(self, fighter_id: int, name: str | None = None, **bio) -> int:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO fighters (id, ufcstats_id, name, dob, height_cm, reach_cm, stance)"
                    " VALUES (:id, :uid, :name, :dob, :h, :r, :stance)"
                ),
                {
                    "id": fighter_id,
                    "uid": f"fighter{fighter_id:04d}",
                    "name": name or f"Fighter {fighter_id}",
                    "dob": bio.get("dob"),
                    "h": bio.get("height_cm"),
                    "r": bio.get("reach_cm"),
                    "stance": bio.get("stance"),
                },
            )
        return fighter_id

    def fight(
        self,
        a: int,
        b: int,
        on: date,
        *,
        winner: int | None = None,
        outcome: str | None = None,
        method: str = "decision",
        rounds: int | None = 3,
        finish_round: int = 3,
        finish_time: int = 300,
        status: str = "completed",
        stats: dict[int, dict] | None = None,
    ) -> int:
        """Insert an event and a fight (one event per fight). Returns the fight id.

        `winner` alone means a win for that fighter; `outcome` alone means a draw, no contest
        or unknown."""
        n = next(self._ids)
        low, high = sorted((a, b))
        completed = status == "completed"
        if completed and outcome is None:
            outcome = "win" if winner else "draw"
        with self.engine.begin() as conn:
            event_id = conn.execute(
                text(
                    "INSERT INTO events (ufcstats_id, name, event_date) VALUES (:u, :n, :d)"
                    " RETURNING id"
                ),
                {"u": f"event{n:04d}", "n": f"Event {n}", "d": on},
            ).scalar_one()
            fight_id = conn.execute(
                text(
                    "INSERT INTO fights (ufcstats_id, event_id, fighter_a_id, fighter_b_id,"
                    " weight_class, gender, status, is_title_fight, outcome, winner_id, method,"
                    " scheduled_rounds, finish_round, finish_time_sec, has_round_stats)"
                    " VALUES (:u, :e, :a, :b, 'Lightweight', 'M', :status, :title, :outcome,"
                    " :winner, :method, :rounds, :fr, :ft, false) RETURNING id"
                ),
                {
                    "u": f"fight{n:04d}",
                    "e": event_id,
                    "a": low,
                    "b": high,
                    "status": status,
                    "title": False if completed else None,
                    "outcome": outcome if completed else None,
                    "winner": winner if completed and outcome == "win" else None,
                    "method": method if completed else None,
                    "rounds": rounds,
                    "fr": finish_round if completed else None,
                    "ft": finish_time if completed else None,
                },
            ).scalar_one()
            for fighter_id, values in (stats or {}).items():
                self._stats(conn, fight_id, fighter_id, values)
        return fight_id

    @staticmethod
    def _stats(conn, fight_id: int, fighter_id: int, values: dict) -> None:
        conn.execute(
            text(
                "INSERT INTO fight_totals (fight_id, fighter_id, sig_strikes_landed,"
                " sig_strikes_att, takedowns_landed, takedowns_att, submission_att, knockdowns)"
                " VALUES (:fight, :fighter, :sl, :sa, :tl, :ta, :sub, :kd)"
            ),
            {
                "fight": fight_id,
                "fighter": fighter_id,
                "sl": values.get("sig_landed"),
                "sa": values.get("sig_att"),
                "tl": values.get("td_landed"),
                "ta": values.get("td_att"),
                "sub": values.get("sub_att"),
                "kd": values.get("knockdowns"),
            },
        )

    def move(self, fight_id: int, to: date) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE events SET event_date = :d"
                    " WHERE id = (SELECT event_id FROM fights WHERE id = :f)"
                ),
                {"d": to, "f": fight_id},
            )

    def sql(self, statement: str, **params) -> None:
        with self.engine.begin() as conn:
            conn.execute(text(statement), params)

    def build(self, today: date = TODAY) -> dict:
        return rebuild(self.engine, today)

    def features(self) -> dict[tuple[int, int], dict]:
        """The feature columns (no built_at / load_run_id) keyed by (fight_id, fighter_id)."""
        columns = ", ".join(FEATURE_COLUMNS)
        with self.engine.connect() as conn:
            rows = conn.execute(text(f"SELECT {columns} FROM fight_features")).mappings()
            return {(r["fight_id"], r["fighter_id"]): dict(r) for r in rows}

    def fight_date(self, fight_id: int) -> date:
        with self.engine.connect() as conn:
            return conn.execute(
                text(
                    "SELECT e.event_date FROM fights f JOIN events e ON e.id = f.event_id"
                    " WHERE f.id = :f"
                ),
                {"f": fight_id},
            ).scalar_one()


@pytest.fixture
def world(db) -> World:
    return World(db)
