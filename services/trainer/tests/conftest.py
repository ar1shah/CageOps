"""A synthetic league written straight into the tables the trainer reads.

The trainer reads `fight_features`, `fights`, `events`, `odds` and `load_runs`, so these tests
write those directly (with a real input fingerprint and a features build row) instead of running
the worker's builder. The signal is strong on purpose: a sign error in the fighter swap would
destroy it, and the accuracy test below would fail.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, text

from cageops_common.db.fingerprint import features_input_fingerprint
from cageops_common.inference.features import NUMERIC_FEATURES

FIRST_EVENT = date(2020, 1, 4)  # a Saturday
EVENTS = 208  # weekly: four years
FIGHTS_PER_EVENT = 3
LAST_EVENT = FIRST_EVENT + timedelta(weeks=EVENTS - 1)

STANCES = ["Orthodox", "Orthodox", "Southpaw", "Switch", None]
WEIGHT_CLASSES = ["Lightweight", "Welterweight", "Middleweight"]


@dataclass
class SyntheticFight:
    fight_id: int
    event_date: date
    a: int
    b: int
    outcome: str  # win | draw | no_contest
    winner: int | None
    method: str
    status: str = "completed"
    weight_class: str = "Lightweight"
    features: dict[int, dict[str, Any]] = field(default_factory=dict)  # fighter -> columns
    odds: tuple[float, float] | None = None


def make_league(seed: int = 7) -> list[SyntheticFight]:
    rng = random.Random(seed)
    fighters = list(range(1, 41))
    skill = {f: rng.gauss(0, 1) for f in fighters}
    bouts: dict[int, int] = dict.fromkeys(fighters, 0)
    fights: list[SyntheticFight] = []
    fight_id = 0
    for week in range(EVENTS):
        day = FIRST_EVENT + timedelta(weeks=week)
        for _ in range(FIGHTS_PER_EVENT):
            fight_id += 1
            a, b = sorted(rng.sample(fighters, 2))
            stronger = a if skill[a] > skill[b] else b
            weaker = b if stronger == a else a
            winner = stronger if rng.random() < 0.9 else weaker
            roll = rng.random()
            method = "ko_tko" if roll < 0.35 else "submission" if roll < 0.55 else "decision"
            fight = SyntheticFight(
                fight_id, day, a, b, "win", winner, method,
                weight_class=rng.choice(WEIGHT_CLASSES),
            )  # fmt: skip
            for fighter in (a, b):
                row: dict[str, Any] = {}
                for name in NUMERIC_FEATURES:
                    if name == "sig_str_landed_pm_career":
                        row[name] = skill[fighter] + rng.gauss(0, 0.3)
                    elif name == "kd_per15_career":
                        row[name] = 0.5 * skill[fighter] + rng.gauss(0, 0.3)
                    elif name in ("prior_ufc_fights", "n_fights_career"):
                        row[name] = bouts[fighter]
                    elif name == "n_fights_last3":
                        row[name] = min(bouts[fighter], 3)
                    elif name == "n_fights_last5":
                        row[name] = min(bouts[fighter], 5)
                    elif rng.random() < 0.1:
                        row[name] = None
                    else:
                        row[name] = rng.gauss(0, 1)
                row["stance"] = rng.choice(STANCES)
                fight.features[fighter] = row
            if (LAST_EVENT - day).days < 500 and rng.random() < 0.8:
                p = 1 / (1 + math.exp(-1.5 * (skill[a] - skill[b])))
                fight.odds = (round(1 / (p * 0.95), 3), round(1 / ((1 - p) * 0.95), 3))
            bouts[a] += 1
            bouts[b] += 1
            fights.append(fight)

    # Rows that must be excluded from training, placed in the training era and in test.
    for index, (outcome, method) in enumerate(
        [("draw", "decision"), ("no_contest", "other"), ("win", "dq"), ("win", "dq")]
    ):
        fights[10 + index * 7].outcome = outcome
        fights[10 + index * 7].method = method
        if outcome != "win":
            fights[10 + index * 7].winner = None
    fights[-1].outcome, fights[-1].winner, fights[-1].method = "draw", None, "decision"

    # An upcoming bout: it has features (the builder makes rows for scheduled fights) but no
    # result, so it can never be a training row.
    upcoming = SyntheticFight(
        fight_id + 1, LAST_EVENT + timedelta(weeks=2), 1, 2, "win", None, "decision",
        status="scheduled",
    )  # fmt: skip
    for fighter in (1, 2):
        upcoming.features[fighter] = {
            **dict.fromkeys(NUMERIC_FEATURES), "stance": "Orthodox",
        }  # fmt: skip
    fights.append(upcoming)
    return fights


class Lab:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.fights = make_league()
        self.run_id: int | None = None

    # -- writing ----------------------------------------------------------------------------

    def reset(self) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "TRUNCATE model_versions, fight_features, odds, fights, events, fighters,"
                    " load_runs, raw_pages RESTART IDENTITY CASCADE"
                )
            )

    def write(self) -> None:
        """(Re)write the whole league, the features rows, and a features build whose stored
        input fingerprint matches what was just written."""
        self.reset()
        with self.engine.begin() as conn:
            for fighter in range(1, 41):
                conn.execute(
                    text(
                        "INSERT INTO fighters (id, ufcstats_id, name, dob, height_cm, reach_cm)"
                        " VALUES (:i, :u, :n, '1990-01-01', 180, 185)"
                    ),
                    {"i": fighter, "u": f"{fighter:016x}", "n": f"Fighter {fighter}"},
                )
            for fight in self.fights:
                event_id = conn.execute(
                    text(
                        "INSERT INTO events (ufcstats_id, name, event_date)"
                        " VALUES (:u, :n, :d)"
                        " ON CONFLICT (ufcstats_id) DO UPDATE SET name = events.name"
                        " RETURNING id"
                    ),
                    {
                        "u": f"{int(fight.event_date.strftime('%Y%m%d')):016x}",
                        "n": f"Event {fight.event_date}",
                        "d": fight.event_date,
                    },
                ).scalar_one()
                done = fight.status == "completed"
                conn.execute(
                    text(
                        "INSERT INTO fights (id, ufcstats_id, event_id, fighter_a_id,"
                        " fighter_b_id, weight_class, gender, status, is_title_fight, outcome,"
                        " winner_id, method, scheduled_rounds, finish_round, finish_time_sec,"
                        " has_round_stats, result_source) VALUES (:id, :u, :e, :a, :b, :wc, 'M',"
                        " :status, :title, :outcome, :winner, :method, 3, :fr, :ft, :done, :src)"
                    ),
                    {
                        "id": fight.fight_id,
                        "u": f"{fight.fight_id + 1000:016x}",
                        "e": event_id,
                        "a": fight.a,
                        "b": fight.b,
                        "wc": fight.weight_class,
                        "status": fight.status,
                        "title": False if done else None,
                        "outcome": fight.outcome if done else None,
                        "winner": fight.winner if done else None,
                        "method": fight.method if done else None,
                        "src": "ufcstats" if done else None,
                        "fr": 3 if done else None,
                        "ft": 300 if done else None,
                        "done": done,
                    },
                )
                if fight.odds:
                    conn.execute(
                        text(
                            "INSERT INTO odds (fight_id, source, decimal_odds_a, decimal_odds_b)"
                            " VALUES (:f, 'mdabbert', :a, :b)"
                        ),
                        {"f": fight.fight_id, "a": fight.odds[0], "b": fight.odds[1]},
                    )
        self.write_build()

    def write_build(self, *, age_hours: int = 1) -> int:
        """A features build row (started `age_hours` ago) whose report stores the CURRENT input
        fingerprint, then the fight_features rows pointing at it."""
        with self.engine.begin() as conn:
            fingerprint = features_input_fingerprint(conn)
            self.run_id = conn.execute(
                text(
                    "INSERT INTO load_runs (source, file_path, sha256, started_at, finished_at,"
                    " report) VALUES ('features', 'db:completed_fights', repeat('c', 64),"
                    " now() - make_interval(hours => :h), now() - make_interval(hours => :h),"
                    " CAST(:r AS jsonb)) RETURNING id"
                ),
                {"h": age_hours, "r": f'{{"inputs_sha256": "{fingerprint}"}}'},
            ).scalar_one()
            conn.execute(text("DELETE FROM fight_features"))
            for fight in self.fights:
                for fighter, row in fight.features.items():
                    columns = {name: row.get(name) for name in NUMERIC_FEATURES}
                    for window in ("career", "last3", "last5"):
                        columns[f"n_fights_{window}"] = row.get(f"n_fights_{window}", 0) or 0
                        columns[f"n_fights_with_stats_{window}"] = 0
                        columns[f"n_fights_with_duration_{window}"] = 0
                    columns["prior_ufc_fights"] = row.get("prior_ufc_fights") or 0
                    names = ", ".join(columns)
                    binds = ", ".join(f":{n}" for n in columns)
                    conn.execute(
                        text(
                            f"INSERT INTO fight_features (fight_id, fighter_id, {names}, stance,"
                            f" weight_class, has_unresolved_prior_fight, load_run_id)"
                            f" VALUES (:fight_id, :fighter_id, {binds}, :stance, :wc, false, :run)"
                        ),
                        {
                            **columns,
                            "fight_id": fight.fight_id,
                            "fighter_id": fighter,
                            "stance": row["stance"],
                            "wc": fight.weight_class,
                            "run": self.run_id,
                        },
                    )
        return self.run_id

    # -- helpers for tests -------------------------------------------------------------------

    def sql(self, statement: str, **params: Any) -> None:
        with self.engine.begin() as conn:
            conn.execute(text(statement), params)

    def default_test_start(self) -> date:
        from cageops_trainer.splits import default_boundaries

        labeled = [
            f.event_date for f in self.fights if f.status == "completed" and f.outcome == "win"
        ]
        return default_boundaries(max(labeled)).test_start


@pytest.fixture
def lab(db) -> Lab:
    league = Lab(db)
    league.write()
    return league
