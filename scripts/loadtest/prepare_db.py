"""Create the load test's own database: a copy of the dev database plus a SYNTHETIC upcoming
card, then a features rebuild so the card has features.

The dev database holds no scheduled bouts (D-044), so without this `/events/upcoming` would be an
empty list in the mixed scenario and the test would not exercise it. The card is labelled in the
event names, lives only in `cageops_loadtest`, and is made of real fighters in real divisions.

    uv run python scripts/loadtest/prepare_db.py [--force]

Refuses to touch any database that is not named cageops_loadtest.
"""

from __future__ import annotations

import argparse
import json
import os
import random
from datetime import timedelta

from sqlalchemy import create_engine, text

from cageops_api.queries import STANDARD_DIVISIONS
from cageops_common.db.session import make_engine
from cageops_common.inference.asof import app_today
from cageops_worker.features.build import rebuild

LOADTEST_DB = "cageops_loadtest"
CARDS = 3
BOUTS_PER_CARD = 12


def admin_url(url: str) -> str:
    head, _, _ = url.rpartition("/")
    return f"{head}/postgres"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--force", action="store_true", help="drop and recreate the database")
    args = parser.parse_args()

    dev_url = os.environ["DATABASE_URL"]
    head, _, dev_name = dev_url.rpartition("/")
    if dev_name == LOADTEST_DB:
        raise SystemExit("DATABASE_URL already points at the load-test database; use the dev one")
    target_url = f"{head}/{LOADTEST_DB}"

    admin = create_engine(
        admin_url(dev_url).replace("postgresql://", "postgresql+psycopg://"),
        isolation_level="AUTOCOMMIT",
    )
    with admin.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": LOADTEST_DB}
        ).scalar()
        if exists and not args.force:
            print(f"{LOADTEST_DB} exists; pass --force to recreate it")
            return 1
        if exists:
            conn.execute(text(f'DROP DATABASE "{LOADTEST_DB}" WITH (FORCE)'))
        # A template copy needs the source to have no other sessions.
        conn.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                " WHERE datname = :d AND pid <> pg_backend_pid()"
            ),
            {"d": dev_name},
        )
        conn.execute(text(f'CREATE DATABASE "{LOADTEST_DB}" TEMPLATE "{dev_name}"'))
    print(f"created {LOADTEST_DB} from {dev_name}")

    engine = make_engine(target_url)
    rng = random.Random(7)
    today = app_today()
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """
                SELECT c.fighter_id, d.weight_class
                  FROM fighter_features_current c
                  JOIN LATERAL (
                    SELECT cf.weight_class FROM completed_fights cf
                      JOIN events e ON e.id = cf.event_id
                     WHERE (cf.fighter_a_id = c.fighter_id OR cf.fighter_b_id = c.fighter_id)
                       AND cf.weight_class = ANY(:divisions)
                     ORDER BY e.event_date DESC, cf.id DESC LIMIT 1) d ON true
                 WHERE c.n_fights_career >= 5 AND c.last_fight_date >= :since
                """
            ),
            {"divisions": list(STANDARD_DIVISIONS), "since": today - timedelta(days=730)},
        ).all()
        pool: dict[str, list[int]] = {}
        for fighter_id, division in rows:
            pool.setdefault(division, []).append(fighter_id)
        for ids in pool.values():
            rng.shuffle(ids)
        divisions = sorted(
            (d for d in pool if len(pool[d]) >= 2 * CARDS * 2), key=lambda d: -len(pool[d])
        )
        made = 0
        for card in range(CARDS):
            event_date = today + timedelta(days=7 * (card + 1))
            event_id = conn.execute(
                text(
                    "INSERT INTO events (ufcstats_id, name, event_date) VALUES (:u, :n, :d)"
                    " RETURNING id"
                ),
                {
                    "u": f"5e00000000000{card:03x}",
                    "n": f"SYNTHETIC LOADTEST CARD {card + 1} (not a real event)",
                    "d": event_date,
                },
            ).scalar_one()
            per_division = BOUTS_PER_CARD // min(len(divisions), 6) or 1
            for division in divisions[:6]:
                for _ in range(per_division):
                    if len(pool[division]) < 2:
                        break
                    a, b = sorted((pool[division].pop(), pool[division].pop()))
                    conn.execute(
                        text(
                            "INSERT INTO fights (event_id, fighter_a_id, fighter_b_id,"
                            " weight_class, gender, status, has_round_stats)"
                            " VALUES (:e, :a, :b, :wc, :g, 'scheduled', false)"
                        ),
                        {
                            "e": event_id,
                            "a": a,
                            "b": b,
                            "wc": division,
                            "g": "F" if division.startswith("Women") else "M",
                        },  # fmt: skip
                    )
                    made += 1
    print(f"inserted {made} scheduled bouts on {CARDS} synthetic cards")

    report = rebuild(engine, today)
    keep = {k: report[k] for k in ("today", "fights_completed", "fights_upcoming", "rows",
                                   "fighters_current", "output_sha256")}  # fmt: skip
    print(json.dumps(keep, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
