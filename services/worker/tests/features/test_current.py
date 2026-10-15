"""fighter_features_current: one row per fighter, as of the build date (D-037).

Rule 1 again (a fight's features may only use data from strictly before its date), for the table
the API reads; plus the guarantee that its two calendar-dependent features can be derived, for any
date, to exactly what the pipeline computes for that date.
"""

from datetime import date, timedelta

import pytest
from sqlalchemy import text

from cageops_common.db.models import FighterFeaturesCurrent, FightFeatures
from cageops_common.inference.asof import date_relative_features
from cageops_worker.features.build import BOOKKEEPING_COLUMNS, CURRENT_COLUMNS
from cageops_worker.features.compute import Subject, compute_features
from cageops_worker.features.history import load_history, prior_fights

ROW = {"sig_landed": 40, "sig_att": 80, "td_landed": 1, "td_att": 3, "sub_att": 1, "knockdowns": 0}
D = date(2026, 6, 1)

# fight_features columns the current table does not carry, and why. A new fight_features column
# fails the test below until it is decided here or added to the current table.
NOT_CURRENT = {
    "fight_id": "a row about a fighter, not a fight",
    "weight_class": "belongs to a fight; an ad-hoc matchup supplies its own",
    "is_title_fight": "belongs to a fight",
    "scheduled_rounds": "belongs to a fight",
    "age_days": "calendar-dependent: derived from dob for the date asked about (D-037)",
    "days_since_last_fight": "calendar-dependent: derived from last_fight_date (D-037)",
}
CURRENT_ONLY = {"built_as_of", "last_fight_date", "dob"}


def current(world) -> dict[int, dict]:
    with world.engine.connect() as conn:
        rows = conn.execute(text("SELECT * FROM fighter_features_current")).mappings()
        return {r["fighter_id"]: dict(r) for r in rows}


def a_career(world):
    for fighter_id, dob in ((1, date(1990, 1, 1)), (2, date(1992, 5, 5)), (3, None)):
        world.fighter(fighter_id, dob=dob, height_cm=180.0, reach_cm=183.0, stance="Orthodox")
    world.fighter(4, dob=date(1995, 1, 1))  # no fights at all
    f1 = world.fight(1, 2, D - timedelta(days=100), winner=1, stats={1: ROW, 2: ROW})
    f2 = world.fight(1, 3, D - timedelta(days=40), winner=3, stats={1: ROW, 3: ROW})
    f3 = world.fight(2, 3, D - timedelta(days=10), winner=2, stats={2: ROW, 3: ROW})
    return f1, f2, f3


# -- what the table is --------------------------------------------------------------------------


def test_every_fight_features_column_is_in_the_current_table_or_excluded_with_a_reason():
    fight_cols = {c.name for c in FightFeatures.__table__.columns} - set(BOOKKEEPING_COLUMNS)
    current_cols = {c.name for c in FighterFeaturesCurrent.__table__.columns} - set(
        BOOKKEEPING_COLUMNS
    )

    assert set(NOT_CURRENT) <= fight_cols
    assert (fight_cols - set(NOT_CURRENT)) | CURRENT_ONLY | {"fighter_id"} == current_cols, (
        f"decide on {sorted(fight_cols - set(NOT_CURRENT) - current_cols)} for "
        "fighter_features_current (add it there or to NOT_CURRENT with a reason)"
    )
    assert all(NOT_CURRENT.values())


def test_the_two_calendar_dependent_features_are_not_stored():
    assert "age_days" not in CURRENT_COLUMNS
    assert "days_since_last_fight" not in CURRENT_COLUMNS
    assert {"dob", "last_fight_date", "built_as_of"} <= set(CURRENT_COLUMNS)


# -- what a build writes ------------------------------------------------------------------------


def test_one_row_per_fighter_including_one_with_no_fights_all_in_the_same_build(world):
    a_career(world)

    report = world.build(D)

    rows = current(world)
    assert sorted(rows) == [1, 2, 3, 4]
    assert report["fighters_current"] == 4 and report["current_as_of"] == D.isoformat()
    assert len(report["current_output_sha256"]) == 64
    with world.engine.connect() as conn:
        run_ids = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT load_run_id FROM fight_features"
                    " UNION SELECT load_run_id FROM fighter_features_current"
                )
            )
        }
    assert len(run_ids) == 1  # one build, one run id, both tables
    assert rows[4]["prior_ufc_fights"] == 0 and rows[4]["last_fight_date"] is None
    assert rows[4]["n_fights_career"] == 0
    assert rows[1]["built_as_of"] == D


def test_the_fight_features_hash_is_not_changed_by_the_new_table(world):
    """`output_sha256` still describes fight_features rows only, so model_versions.
    features_output_sha256 keeps its meaning."""
    a_career(world)
    first = world.build(D)
    with world.engine.begin() as conn:
        conn.execute(text("DELETE FROM fighter_features_current"))
    second = world.build(D)

    assert first["output_sha256"] == second["output_sha256"]
    assert first["current_output_sha256"] == second["current_output_sha256"]


def test_a_failed_build_leaves_the_current_table_as_it_was(world, monkeypatch):
    from cageops_worker.features import build as build_module

    a_career(world)
    world.build(D)
    before = current(world)
    real = build_module.build_rows
    monkeypatch.setattr(build_module, "build_rows", lambda *a, **k: real(*a, **k)[:-1])
    with pytest.raises(build_module.FeatureBuildError):
        world.build(D + timedelta(days=30))

    assert current(world) == before


# -- rule 1 -------------------------------------------------------------------------------------


def test_a_fight_dated_today_is_not_history_and_one_dated_yesterday_is(world):
    world.fighter(1, dob=date(1990, 1, 1))
    world.fighter(2, dob=date(1991, 1, 1))
    world.fight(1, 2, D - timedelta(days=1), winner=1)  # yesterday
    world.fight(1, 2, D, winner=2)  # today's card

    world.build(D)

    row = current(world)[1]
    assert row["n_fights_career"] == 1
    assert row["prior_ufc_fights"] == 1
    assert row["last_fight_date"] == D - timedelta(days=1)

    world.build(D + timedelta(days=1))  # a day later today's fight is history

    row = current(world)[1]
    assert row["n_fights_career"] == 2
    assert row["last_fight_date"] == D


def test_rewriting_fights_dated_today_or_later_cannot_change_the_current_rows(world):
    a_career(world)
    world.fight(1, 2, D, winner=1, stats={1: ROW, 2: ROW})  # today
    world.fight(2, 3, D + timedelta(days=20), winner=2, stats={2: ROW, 3: ROW})  # later
    world.build(D)
    before = current(world)

    world.sql(
        "UPDATE fight_totals SET sig_strikes_landed = sig_strikes_landed * 3 + 7,"
        " knockdowns = 4 WHERE fight_id IN (SELECT f.id FROM fights f JOIN events e"
        " ON e.id = f.event_id WHERE e.event_date >= :d)",
        d=D,
    )
    world.sql(
        "UPDATE fights SET winner_id = fighter_b_id WHERE winner_id = fighter_a_id AND id IN"
        " (SELECT f.id FROM fights f JOIN events e ON e.id = f.event_id WHERE e.event_date >= :d)",
        d=D,
    )
    world.build(D)

    assert {k: _drop_bookkeeping(v) for k, v in current(world).items()} == {
        k: _drop_bookkeeping(v) for k, v in before.items()
    }


def _drop_bookkeeping(row: dict) -> dict:
    return {k: v for k, v in row.items() if k not in BOOKKEEPING_COLUMNS}


# -- the pipeline and the current table agree ---------------------------------------------------


def test_a_scheduled_fight_dated_on_the_build_date_has_the_same_features_as_the_current_row(
    world,
):
    """The strongest consistency check: build as of D, with a bout scheduled on D. The history-
    derived columns of that bout's fight_features rows equal the fighters' current rows, and the
    two calendar-dependent ones equal what the shared function derives from the current row."""
    a_career(world)
    scheduled = world.fight(1, 2, D, status="scheduled")
    world.build(D)

    cur = current(world)
    with world.engine.connect() as conn:
        feature_rows = {
            r["fighter_id"]: dict(r)
            for r in conn.execute(
                text("SELECT * FROM fight_features WHERE fight_id = :f"), {"f": scheduled}
            ).mappings()
        }
    assert sorted(feature_rows) == [1, 2]
    for fighter_id, row in feature_rows.items():
        mine = cur[fighter_id]
        for column in CURRENT_COLUMNS:
            if column in CURRENT_ONLY or column == "fighter_id":
                continue
            assert row[column] == mine[column], column
        derived = date_relative_features(D, mine["dob"], mine["last_fight_date"])
        assert row["age_days"] == derived["age_days"]
        assert row["days_since_last_fight"] == derived["days_since_last_fight"]


@pytest.mark.parametrize("as_of", [D, D + timedelta(days=1), D + timedelta(days=45)])
def test_the_derived_features_match_what_the_pipeline_computes_for_the_same_date(world, as_of):
    """Requested test: the API's date-relative values for an as-of date equal `compute_features`
    for that date, for every fighter, including ones with no dob and no fights."""
    a_career(world)
    world.build(as_of)  # current rows built as of the date being asked about
    cur = current(world)
    with world.engine.connect() as conn:
        history = load_history(conn)

    for fighter_id, row in cur.items():
        bio = history.bios[fighter_id]
        pipeline = compute_features(
            prior_fights(history, fighter_id, as_of),
            Subject(as_of, bio.dob, bio.height_cm, bio.reach_cm, bio.stance, None, None, None),
        )
        served = date_relative_features(as_of, row["dob"], row["last_fight_date"])
        assert served["age_days"] == pipeline["age_days"], fighter_id
        assert served["days_since_last_fight"] == pipeline["days_since_last_fight"], fighter_id


def test_one_stored_row_asked_a_day_later_moves_both_features_by_one_and_nothing_else(world):
    a_career(world)
    world.build(D)
    row = current(world)[1]

    today = date_relative_features(D, row["dob"], row["last_fight_date"])
    tomorrow = date_relative_features(D + timedelta(days=1), row["dob"], row["last_fight_date"])

    assert tomorrow["age_days"] == today["age_days"] + 1
    assert tomorrow["days_since_last_fight"] == today["days_since_last_fight"] + 1
