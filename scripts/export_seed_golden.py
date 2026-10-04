"""Export the Phase 1a seed's rows for the fixture fights to a small JSON file.

    uv run python scripts/export_seed_golden.py

The seed-vs-scrape test (services/worker/tests/ingest/test_seed_match.py) loads this file into the
test database as "the seed", scrapes the saved pages over it, and compares every column. The data
isn't in CI (data/ is gitignored), so this snapshot is what makes the comparison reproducible.
It holds a handful of rows of facts from the CC0 silver dataset. Re-run it only if the seed is
reloaded from different files (the manifest hash changes); then re-check the test's allow-list.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, text

from cageops_common.config import get_settings
from cageops_common.db.session import make_engine
from cageops_worker.ingest.mapping import BREAKDOWN_STATS, CORE_STATS

OUT = Path("services/worker/tests/fixtures/seed_golden.json")

# The fights we have saved ufcstats pages for, and fighters whose pages we saved.
FIGHT_IDS = [
    "32054bf2b36b0e47",  # Burns vs Malott
    "b5299e5b946015e5",  # Phillips vs Jourdain
    "9fab4b0ad082f670",  # Valentin vs Leblanc
    "552f7cdaf93e1055",  # Vologdin vs Castaneda
    "6390c8e74630473e",  # Aspinall vs Gane
    "635fbf57001897c7",  # Santos vs Marscucci, 1998
]
EXTRA_FIGHTER_IDS = ["6eedb757f13b9978", "53e533db1b8e9712", "6cbb7661c3258617"]  # bios

STAT_CORE = list(CORE_STATS)
STAT_BREAKDOWN = list(BREAKDOWN_STATS)


def read_rows(conn: Connection, fight_ids: list[str], fighter_ids: list[str]) -> dict[str, Any]:
    """The rows for these fights, keyed by ufcstats ids (never database ids), in a stable order.
    Used both to export the seed and, in the test, to read back what the scraper wrote."""
    fights = conn.execute(
        text(
            """
            SELECT f.ufcstats_id, e.ufcstats_id AS event, a.ufcstats_id AS fighter_a,
                   b.ufcstats_id AS fighter_b, r.ufcstats_id AS red, w.ufcstats_id AS winner,
                   f.status, f.weight_class, f.gender, f.is_title_fight, f.scheduled_rounds,
                   f.outcome, f.method, f.decision_type, f.method_detail, f.finish_round,
                   f.finish_time_sec, f.referee, f.has_round_stats
            FROM fights f JOIN events e ON e.id = f.event_id
            JOIN fighters a ON a.id = f.fighter_a_id JOIN fighters b ON b.id = f.fighter_b_id
            LEFT JOIN fighters r ON r.id = f.red_fighter_id
            LEFT JOIN fighters w ON w.id = f.winner_id
            WHERE f.ufcstats_id = ANY(:ids) ORDER BY f.ufcstats_id
            """
        ),
        {"ids": fight_ids},
    ).mappings().all()  # fmt: skip
    events = sorted({row["event"] for row in fights})
    participants = sorted({row[k] for row in fights for k in ("fighter_a", "fighter_b")})
    all_fighters = sorted(set(participants) | set(fighter_ids))
    return {
        "events": [
            dict(r)
            for r in conn.execute(
                text(
                    "SELECT ufcstats_id, name, event_date, city, state, country FROM events"
                    " WHERE ufcstats_id = ANY(:ids) ORDER BY ufcstats_id"
                ),
                {"ids": events},
            ).mappings()
        ],
        "fighters": [
            dict(r)
            for r in conn.execute(
                text(
                    "SELECT ufcstats_id, name, dob, height_cm, reach_cm, stance FROM fighters"
                    " WHERE ufcstats_id = ANY(:ids) ORDER BY ufcstats_id"
                ),
                {"ids": all_fighters},
            ).mappings()
        ],
        "fights": [dict(r) for r in fights],
        "fight_totals": _stats(conn, "fight_totals", STAT_CORE, fight_ids, rounds=False),
        "fight_round_stats": _stats(
            conn, "fight_round_stats", STAT_CORE + STAT_BREAKDOWN, fight_ids, rounds=True
        ),
    }


def _stats(conn, table, columns, fight_ids, *, rounds) -> list[dict[str, Any]]:
    select = ", ".join(f"t.{c}" for c in columns)
    order = "f.ufcstats_id, p.ufcstats_id" + (", t.round" if rounds else "")
    rows = conn.execute(
        text(
            f"SELECT f.ufcstats_id AS fight, p.ufcstats_id AS fighter,"
            f"{' t.round,' if rounds else ''} {select} FROM {table} t"
            " JOIN fights f ON f.id = t.fight_id JOIN fighters p ON p.id = t.fighter_id"
            f" WHERE f.ufcstats_id = ANY(:ids) ORDER BY {order}"
        ),
        {"ids": fight_ids},
    ).mappings()
    return [dict(r) for r in rows]


def main() -> None:
    engine = make_engine(get_settings().database_url)
    with engine.connect() as conn:
        rows = read_rows(conn, FIGHT_IDS, EXTRA_FIGHTER_IDS)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(rows, indent=1, sort_keys=True, default=str) + "\n", encoding="utf-8")
    print(
        f"wrote {OUT}: {len(rows['events'])} events, {len(rows['fighters'])} fighters, "
        f"{len(rows['fights'])} fights, {len(rows['fight_totals'])} totals rows, "
        f"{len(rows['fight_round_stats'])} round rows"
    )


if __name__ == "__main__":
    main()
