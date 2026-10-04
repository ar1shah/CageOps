"""Scraped rows must line up with seed rows for the same fight (Phase 1b requirement).

The golden file is the real Phase 1a seed's rows for the fixture fights (see
scripts/export_seed_golden.py). This test loads it as "the seed", runs the ingestion jobs over the
saved ufcstats pages, and compares EVERY column of every row. The only differences allowed are the
ones in ALLOWED below, each one explained; anything else, or an allowance that no longer applies,
fails the test.
"""

import json
from datetime import date
from pathlib import Path

import pytest
from export_seed_golden import EXTRA_FIGHTER_IDS, FIGHT_IDS, read_rows
from sqlalchemy import insert

from cageops_common.db.models import (
    Event,
    Fight,
    Fighter,
    FightRoundStats,
    FightTotals,
)
from cageops_worker.ingest import jobs

from .conftest import BASE, BURNS_CARD

GOLDEN = Path(__file__).parents[1] / "fixtures" / "seed_golden.json"
RAHIKI = "6eedb757f13b9978"

# (table, row key, column) -> (the seed's value, the scraped value), and WHY they differ.
ALLOWED = {
    # The seed loader couldn't read a winner for this majority draw (silver's winner column matched
    # neither fighter), so it stored 'unknown'. The fight page says draw, which is right.
    ("fights", "552f7cdaf93e1055", "outcome"): ("unknown", "draw"),
    # 1998, "1 Rnd + OT (12-3)": the page says the fight ended at 10:27 (627 s); the seed holds 27,
    # which looks like a parse of "0:27". The page is the primary source.
    ("fights", "635fbf57001897c7", "finish_time_sec"): (27, 627),
    # Marwan Rahiki: the seed (a June 2026 snapshot) has no bio; the site has since published one.
    ("fighters", RAHIKI, "height_cm"): (None, 172.72),
    ("fighters", RAHIKI, "reach_cm"): (None, 182.88),
    ("fighters", RAHIKI, "dob"): (None, "2002-05-10"),
    ("fighters", RAHIKI, "stance"): (None, "Orthodox"),
}
EXPECTED_RESULT_CHANGES = {
    "result_changed:fight=552f7cdaf93e1055:outcome=unknown->draw",
    "result_changed:fight=635fbf57001897c7:finish_time_sec=27->627",
}
KEYS = {
    "events": ("ufcstats_id",),
    "fighters": ("ufcstats_id",),
    "fights": ("ufcstats_id",),
    "fight_totals": ("fight", "fighter"),
    "fight_round_stats": ("fight", "fighter", "round"),
}


@pytest.fixture(scope="module")
def golden():
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def seed_database(db, golden):
    """Load the golden rows the way the Phase 1a loader laid them out: fighter ids assigned in
    ufcstats-id order, so fighter_a < fighter_b follows that order."""
    with db.begin() as conn:
        conn.execute(
            insert(Event.__table__),
            [{**e, "event_date": date.fromisoformat(e["event_date"])} for e in golden["events"]],
        )
        conn.execute(
            insert(Fighter.__table__),
            [
                {**f, "dob": date.fromisoformat(f["dob"]) if f["dob"] else None}
                for f in sorted(golden["fighters"], key=lambda f: f["ufcstats_id"])
            ],
        )
        fighters = dict(conn.execute(Fighter.__table__.select().with_only_columns(
            Fighter.__table__.c.ufcstats_id, Fighter.__table__.c.id)).all())  # fmt: skip
        events = dict(conn.execute(Event.__table__.select().with_only_columns(
            Event.__table__.c.ufcstats_id, Event.__table__.c.id)).all())  # fmt: skip
        fight_rows = []
        for f in golden["fights"]:
            row = {
                k: v
                for k, v in f.items()
                if k not in ("event", "fighter_a", "fighter_b", "red", "winner")
            }
            row |= {
                "event_id": events[f["event"]],
                "fighter_a_id": fighters[f["fighter_a"]],
                "fighter_b_id": fighters[f["fighter_b"]],
                "red_fighter_id": fighters[f["red"]] if f["red"] else None,
                "winner_id": fighters[f["winner"]] if f["winner"] else None,
            }
            fight_rows.append(row)
        conn.execute(insert(Fight.__table__), fight_rows)
        fights = dict(conn.execute(Fight.__table__.select().with_only_columns(
            Fight.__table__.c.ufcstats_id, Fight.__table__.c.id)).all())  # fmt: skip
        for table_name, model in (
            ("fight_totals", FightTotals),
            ("fight_round_stats", FightRoundStats),
        ):
            rows = [
                {k: v for k, v in r.items() if k not in ("fight", "fighter")}
                | {"fight_id": fights[r["fight"]], "fighter_id": fighters[r["fighter"]]}
                for r in golden[table_name]
            ]
            conn.execute(insert(model.__table__), rows)


def scrape(ctx, run_jobs):
    """Run the real jobs over the saved pages, over the seeded database."""
    jobs.fetch_event(f"{BASE}/event-details/{BURNS_CARD}", "2026-04-18", "match")  # 4 fights here
    run_jobs()  # their fight jobs and fighter jobs (pages we don't have 404 into the DLQ)
    # Two more fights whose event pages we didn't save: their event rows come from the seed.
    for fight_id, event_name, event_date in (
        ("6390c8e74630473e", "UFC 321: Aspinall vs. Gane", "2025-10-25"),
        ("635fbf57001897c7", "UFC - Ultimate Brazil", "1998-10-16"),
    ):
        event_id = _event_id(ctx, event_name)
        jobs.fetch_fight(f"{BASE}/fight-details/{fight_id}", event_id, event_date, None, "match")
    for fighter_id in (RAHIKI, "53e533db1b8e9712", "6cbb7661c3258617"):
        jobs.fetch_fighter(f"{BASE}/fighter-details/{fighter_id}", "match")


def _event_id(ctx, name):
    with ctx.engine.connect() as conn:
        return conn.exec_driver_sql(
            "SELECT ufcstats_id FROM events WHERE name = %s", (name,)
        ).scalar_one()


def normalized(rows):
    return json.loads(json.dumps(rows, default=str))


def read_back(ctx):
    with ctx.engine.connect() as conn:
        return normalized(read_rows(conn, FIGHT_IDS, EXTRA_FIGHTER_IDS))


def keyed(rows, key_columns):
    return {tuple(r[c] for c in key_columns): r for r in rows}


def differences(seed, scraped):
    """{(table, key, column): (seed value, scraped value)}, after checking both sides hold the
    same rows."""
    found = {}
    for table, key_columns in KEYS.items():
        seed_rows = keyed(seed[table], key_columns)
        scraped_rows = keyed(scraped[table], key_columns)
        assert set(seed_rows) == set(scraped_rows), f"{table}: different rows"
        for key, seed_row in seed_rows.items():
            for column, seed_value in seed_row.items():
                scraped_value = scraped_rows[key][column]
                if seed_value != scraped_value:
                    label = key[0] if len(key) == 1 else "/".join(map(str, key))
                    found[(table, label, column)] = (seed_value, scraped_value)
    return found


def test_scraped_rows_match_the_seed_except_for_the_documented_differences(
    ctx, run_jobs, golden, db, caplog
):
    seed_database(db, golden)

    with caplog.at_level("WARNING"):
        scrape(ctx, run_jobs)

    assert differences(golden, read_back(ctx)) == ALLOWED
    changed = {
        getattr(r, "detail", "")
        for r in caplog.records
        if getattr(r, "detail", "").startswith("result_changed")
    }
    assert changed == EXPECTED_RESULT_CHANGES  # the seed's two result differences were reported


def test_every_fixture_fight_was_compared_with_all_of_its_stat_rows(ctx, run_jobs, golden, db):
    seed_database(db, golden)
    scrape(ctx, run_jobs)

    scraped = read_back(ctx)

    assert len(scraped["fights"]) == 6
    assert len(scraped["fight_totals"]) == 10  # five fights with stats, two fighters each
    assert len(scraped["fight_round_stats"]) == 22
    assert {f["status"] for f in scraped["fights"]} == {"completed"}


def test_scraping_over_the_seed_a_second_time_changes_nothing_more(
    ctx, run_jobs, golden, db, caplog, snapshot
):
    seed_database(db, golden)
    scrape(ctx, run_jobs)
    first = snapshot()
    caplog.clear()

    with caplog.at_level("WARNING"):
        scrape(ctx, run_jobs)

    assert snapshot() == first
    assert not [r for r in caplog.records if "result_changed" in getattr(r, "detail", "")]
