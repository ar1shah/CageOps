import re
from datetime import date, timedelta

import pytest
from sqlalchemy import text

from cageops_common.db.rankings import RANKING_MAX_AGE_DAYS, RANKING_SOURCE_PRIORITY
from cageops_worker.seed.rankings import RankingRow, clean_snapshots, load_rankings, read_rankings

FIGHT_DATE = date(2024, 6, 1)


def rrow(day, name="Alice A", rank=1, wc="Lightweight", kind="division"):
    return RankingRow(day, kind, wc, name, rank)


# ---- pure: the quality rule ----


def test_snapshot_with_a_fighter_listed_twice_is_dropped_whole():
    good = date(2024, 1, 1)
    merged = date(2024, 1, 8)
    rows = [
        rrow(good, "Alice A", 1),
        rrow(good, "Bob B", 2),
        rrow(merged, "Alice A", 1),
        rrow(merged, "Alice A", 3),  # same fighter twice in one list: two lists merged
        rrow(merged, "Cara C", 2, wc="Flyweight"),  # innocent bystander on the same date
    ]

    kept, dropped = clean_snapshots(rows)

    assert dropped == [merged]
    assert {r.snapshot_date for r in kept} == {good}


def test_same_fighter_in_different_lists_is_not_a_duplicate():
    day = date(2024, 1, 1)
    rows = [
        rrow(day, "Alice A", 0, wc="Lightweight"),
        rrow(day, "Alice A", 1, wc="Men's Pound-for-Pound", kind="pound_for_pound"),
    ]

    kept, dropped = clean_snapshots(rows)

    assert dropped == [] and len(kept) == 2


def test_spelling_variants_of_the_same_fighter_still_count_as_duplicates():
    day = date(2024, 1, 1)
    rows = [rrow(day, "José Aldo", 0), rrow(day, "Jose Aldo", 2)]

    _, dropped = clean_snapshots(rows)

    assert dropped == [day]


def test_read_rankings_maps_weight_classes_and_skips_vacant_title_rows(tmp_path):
    path = tmp_path / "r.csv"
    path.write_text(
        "date,weightclass,fighter,rank\n"
        "2024-01-01,Lightweight,Alice A,1\n"
        "2024-01-01,Lightweight,,0\n"  # vacant title (empty name)
        "2024-01-01,Lightweight,NA,0\n"  # vacant title (NA)
        "2024-01-01,Pound-for-Pound,Bob B,1\n"
    )

    rows, vacant = read_rankings(path)

    assert vacant == 2
    assert [(r.ranking_type, r.weight_class) for r in rows] == [
        ("division", "Lightweight"),
        ("pound_for_pound", "Men's Pound-for-Pound"),
    ]


def test_read_rankings_fails_on_an_unknown_weight_class(tmp_path):
    path = tmp_path / "r.csv"
    path.write_text("date,weightclass,fighter,rank\n2024-01-01,Super Heavyweight,A B,1\n")

    with pytest.raises(ValueError, match="unknown weight class"):
        read_rankings(path)


# ---- integration: loader + the fight_pre_rankings view (real Postgres) ----


@pytest.fixture
def world(db):
    """Fighter 1 (Alice) vs fighter 2 (Bob), a lightweight fight on FIGHT_DATE."""
    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO fighters (id, ufcstats_id, name) VALUES"
                " (1, 'aaaa000000000001', 'Alice A'), (2, 'bbbb000000000002', 'Bob B')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO events (id, ufcstats_id, name, event_date)"
                " VALUES (1, 'e100000000000001', 'E', :d)"
            ),
            {"d": FIGHT_DATE},
        )
        conn.execute(
            text(
                "INSERT INTO fights (ufcstats_id, event_id, fighter_a_id, fighter_b_id,"
                " weight_class, gender, is_title_fight, outcome, winner_id, method,"
                " has_round_stats, result_source) VALUES ('f100000000000001', 1, 1, 2,"
                " 'Lightweight', 'M', false, 'win', 1, 'decision', false, 'ufcstats')"
            )
        )
    return db


def add_rank(
    db, day, fighter_id, name, rank, source="jerzyszocik", wc="Lightweight", kind="division"
):
    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO rankings (source, snapshot_date, ranking_type, weight_class,"
                " name_raw, fighter_id, rank) VALUES (:s, :d, :k, :w, :n, :f, :r)"
            ),
            {"s": source, "d": day, "k": kind, "w": wc, "n": name, "f": fighter_id, "r": rank},
        )


def view_row(db, fighter_id=1, kind="division"):
    with db.connect() as conn:
        return conn.execute(
            text(
                "SELECT status, rank, snapshot_date, source, snapshot_age_days"
                " FROM fight_pre_rankings WHERE fighter_id = :f AND ranking_type = :k"
            ),
            {"f": fighter_id, "k": kind},
        ).one()


def test_ranking_published_on_the_fight_date_is_not_used(world):
    add_rank(world, FIGHT_DATE - timedelta(days=7), 1, "Alice A", 5)
    add_rank(world, FIGHT_DATE, 1, "Alice A", 1)  # same day as the fight: too late

    row = view_row(world)

    assert (row.status, row.rank) == ("ranked", 5)
    assert row.snapshot_date == FIGHT_DATE - timedelta(days=7)


def test_changing_a_future_snapshot_does_not_change_the_pre_fight_rank(world):
    """Point-in-time proof: rewrite everything after the fight, the answer must not move."""
    add_rank(world, FIGHT_DATE - timedelta(days=3), 1, "Alice A", 4)
    add_rank(world, FIGHT_DATE + timedelta(days=4), 1, "Alice A", 9)
    before = view_row(world)

    with world.begin() as conn:
        conn.execute(
            text("UPDATE rankings SET rank = 0 WHERE snapshot_date > :d"), {"d": FIGHT_DATE}
        )
        conn.execute(
            text(
                "INSERT INTO rankings (source, snapshot_date, ranking_type, weight_class,"
                " name_raw, fighter_id, rank) VALUES ('jerzyszocik', :d, 'division',"
                " 'Lightweight', 'Alice A', 1, 1) ON CONFLICT DO NOTHING"
            ),
            {"d": FIGHT_DATE + timedelta(days=11)},
        )

    assert view_row(world) == before


def test_staleness_cap_is_inclusive_at_the_limit_and_null_beyond_it(world):
    add_rank(world, FIGHT_DATE - timedelta(days=RANKING_MAX_AGE_DAYS), 1, "Alice A", 3)
    at_limit = view_row(world)

    with world.begin() as conn:
        conn.execute(text("DELETE FROM rankings"))
    add_rank(world, FIGHT_DATE - timedelta(days=RANKING_MAX_AGE_DAYS + 1), 1, "Alice A", 3)
    beyond = view_row(world)

    assert (at_limit.status, at_limit.rank) == ("ranked", 3)
    assert (beyond.status, beyond.rank) == ("stale", None)
    assert beyond.snapshot_age_days == RANKING_MAX_AGE_DAYS + 1  # age is still reported


def test_view_definition_uses_the_named_staleness_constant(world):
    with world.connect() as conn:
        definition = conn.execute(
            text("SELECT pg_get_viewdef('fight_pre_rankings'::regclass)")
        ).scalar_one()

    ages = set(re.findall(r"snapshot_date\)\s*([<>]=?)\s*(\d+)", definition))
    assert ages and {n for _, n in ages} == {str(RANKING_MAX_AGE_DAYS)}


def test_fighter_missing_from_the_latest_snapshot_is_unranked_not_given_an_older_rank(world):
    add_rank(world, FIGHT_DATE - timedelta(days=14), 1, "Alice A", 2)  # older: Alice was #2
    add_rank(world, FIGHT_DATE - timedelta(days=7), 2, "Bob B", 1)  # latest list: no Alice

    row = view_row(world, fighter_id=1)

    assert (row.status, row.rank) == ("unranked", None)


def test_no_snapshot_before_the_fight(world):
    add_rank(world, FIGHT_DATE + timedelta(days=7), 1, "Alice A", 2)

    assert view_row(world).status == "no_snapshot"


def test_prefers_the_primary_source_when_both_are_fresh(world):
    add_rank(world, FIGHT_DATE - timedelta(days=10), 1, "Alice A", 6, source="jerzyszocik")
    add_rank(world, FIGHT_DATE - timedelta(days=2), 1, "Alice A", 2, source="martj42")

    row = view_row(world)

    assert RANKING_SOURCE_PRIORITY[0] == "jerzyszocik"
    assert (row.source, row.rank) == ("jerzyszocik", 6)


def test_falls_back_to_the_second_source_when_the_first_is_stale(world):
    add_rank(world, FIGHT_DATE - timedelta(days=60), 1, "Alice A", 6, source="jerzyszocik")
    add_rank(world, FIGHT_DATE - timedelta(days=5), 1, "Alice A", 4, source="martj42")

    row = view_row(world)

    assert (row.source, row.status, row.rank) == ("martj42", "ranked", 4)


def test_pound_for_pound_uses_the_list_matching_the_fights_gender(world):
    add_rank(
        world, FIGHT_DATE - timedelta(days=7), 1, "Alice A", 3,
        wc="Men's Pound-for-Pound", kind="pound_for_pound",
    )  # fmt: skip
    add_rank(
        world, FIGHT_DATE - timedelta(days=7), 2, "Bob B", 1,
        wc="Women's Pound-for-Pound", kind="pound_for_pound",
    )  # fmt: skip

    alice = view_row(world, fighter_id=1, kind="pound_for_pound")
    bob = view_row(world, fighter_id=2, kind="pound_for_pound")

    assert (alice.status, alice.rank) == ("ranked", 3)
    assert bob.status == "unranked"  # the fight is a men's fight; Bob is on the women's list


def write_rankings(tmp_path, lines):
    path = tmp_path / "r.csv"
    path.write_text("date,weightclass,fighter,rank\n" + "\n".join(lines) + "\n")
    return path


def test_load_skips_merged_dates_resolves_names_and_is_idempotent(world, tmp_path):
    path = write_rankings(
        tmp_path,
        [
            "2024-05-06,Lightweight,Alice A,0",
            "2024-05-06,Lightweight,Bob B,1",
            "2024-05-06,Lightweight,Unknown Guy,2",
            "2024-05-13,Lightweight,Alice A,0",
            "2024-05-13,Lightweight,Alice A,1",  # merged lists: whole date skipped
            "2024-05-13,Lightweight,Bob B,2",
        ],
    )

    first = load_rankings(world, "jerzyszocik", path, "sha", aliases_csv=None)
    load_rankings(world, "jerzyszocik", path, "sha", aliases_csv=None)

    assert first["snapshot_dates_loaded"] == 1
    assert first["snapshot_dates_skipped_merged_lists"] == 1
    assert first["names_unmatched"] == 1 and first["unmatched_names"][0]["name"] == "Unknown Guy"
    with world.connect() as conn:
        rows = conn.execute(text("SELECT name_raw, fighter_id FROM rankings ORDER BY rank")).all()
    assert rows == [("Alice A", 1), ("Bob B", 2), ("Unknown Guy", None)]  # unmatched row is kept
