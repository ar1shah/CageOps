"""The rebuild: idempotence, its own checks, upcoming and stale scheduled fights, the schema."""

from datetime import date, timedelta

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from cageops_worker.features import build as build_module
from cageops_worker.features.build import BOOKKEEPING_COLUMNS, FEATURE_COLUMNS, FeatureBuildError

ROW = {"sig_landed": 40, "sig_att": 80, "td_landed": 1, "td_att": 3, "sub_att": 1, "knockdowns": 0}


def small_world(world):
    for fighter_id in (1, 2, 3):
        world.fighter(fighter_id)
    f1 = world.fight(1, 2, date(2021, 1, 1), winner=1, stats={1: ROW, 2: ROW})
    f2 = world.fight(1, 3, date(2021, 6, 1), winner=3, method="ko_tko", stats={1: ROW, 3: ROW})
    f3 = world.fight(2, 3, date(2021, 12, 1), winner=2, stats={2: ROW, 3: ROW})
    return f1, f2, f3


def count(world, sql, **params):
    with world.engine.connect() as conn:
        return conn.execute(text(sql), params).scalar_one()


# -- rebuild ---------------------------------------------------------------------------------


def test_every_fight_gets_exactly_two_rows(world):
    fights = small_world(world)

    report = world.build()

    assert report["rows"] == 6
    assert report["fights_without_exactly_two_rows"] == []
    for fight_id in fights:
        assert (
            count(world, "SELECT count(*) FROM fight_features WHERE fight_id = :f", f=fight_id) == 2
        )


def test_rebuilding_twice_gives_identical_features_and_fingerprint(world):
    small_world(world)

    first = world.build()
    features_first = world.features()
    second = world.build()

    assert world.features() == features_first  # FEATURE_COLUMNS: no built_at, no load_run_id
    assert first["output_sha256"] == second["output_sha256"]
    assert count(world, "SELECT count(*) FROM fight_features") == 6  # replaced, not appended
    assert count(world, "SELECT count(*) FROM load_runs WHERE source = 'features'") == 2
    assert count(world, "SELECT count(DISTINCT load_run_id) FROM fight_features") == 1


def test_the_load_run_records_what_was_built(world):
    small_world(world)

    report = world.build()

    with world.engine.connect() as conn:
        run = (
            conn.execute(text("SELECT * FROM load_runs WHERE source = 'features'")).mappings().one()
        )
    assert run["rows_loaded"] == 6
    assert run["finished_at"] is not None
    assert run["sha256"] == report["output_sha256"]
    assert run["report"]["rows"] == 6


def test_a_failed_check_aborts_and_leaves_the_old_table_and_no_load_run(world, monkeypatch):
    small_world(world)
    world.build()
    before = world.features()
    runs_before = count(world, "SELECT count(*) FROM load_runs")
    real = build_module.build_rows
    monkeypatch.setattr(build_module, "build_rows", lambda *a, **k: real(*a, **k)[:-1])

    with pytest.raises(FeatureBuildError) as caught:
        world.build()

    assert len(caught.value.report["fights_without_exactly_two_rows"]) == 1
    assert world.features() == before
    assert count(world, "SELECT count(*) FROM load_runs") == runs_before


def test_the_table_has_foreign_keys_to_fights_fighters_and_load_runs(db):
    foreign_keys = inspect(db).get_foreign_keys("fight_features")

    assert {(tuple(fk["constrained_columns"]), fk["referred_table"]) for fk in foreign_keys} == {
        (("fight_id",), "fights"),
        (("fighter_id",), "fighters"),
        (("load_run_id",), "load_runs"),
    }


def test_feature_columns_are_every_table_column_except_the_bookkeeping_two(db):
    table_columns = {c["name"] for c in inspect(db).get_columns("fight_features")}
    assert set(FEATURE_COLUMNS) == table_columns - set(BOOKKEEPING_COLUMNS)


def test_a_row_cannot_point_at_a_fight_that_does_not_exist(world):
    small_world(world)
    world.build()
    run_id = count(world, "SELECT max(id) FROM load_runs")

    with pytest.raises(IntegrityError, match="foreign key"):
        world.sql(
            "INSERT INTO fight_features (fight_id, fighter_id, n_fights_career,"
            " n_fights_with_stats_career, n_fights_with_duration_career, n_fights_last3,"
            " n_fights_with_stats_last3, n_fights_with_duration_last3, n_fights_last5,"
            " n_fights_with_stats_last5, n_fights_with_duration_last5, prior_ufc_fights,"
            " has_unresolved_prior_fight, load_run_id)"
            " VALUES (9999, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, false, :run)",
            run=run_id,
        )


# -- report ----------------------------------------------------------------------------------


def test_the_report_counts_fights_with_no_trusted_duration_by_reason(world):
    for fighter_id in (1, 2, 3, 4, 5):
        world.fighter(fighter_id)
    world.fight(1, 2, date(2021, 1, 1), winner=1)  # fine
    world.fight(1, 3, date(2021, 2, 1), winner=1, rounds=1, finish_round=1)  # not a standard format
    world.fight(1, 4, date(2021, 3, 1), winner=1, rounds=None)  # no scheduled rounds
    world.fight(1, 5, date(2021, 4, 1), winner=1, finish_time=115)  # decision, not full length

    nulls = world.build()["completed_fights_with_null_duration"]

    assert nulls == {
        "total": 3,
        "format_not_standard": 2,
        "finish_round_out_of_range": 0,
        "finish_time_out_of_range": 0,
        "decision_not_full_length": 1,
    }


def test_history_through_is_the_latest_completed_event(world):
    small_world(world)
    assert world.build()["history_through"] == "2021-12-01"


# -- upcoming and stale scheduled fights (#4) ------------------------------------------------


def test_an_upcoming_scheduled_fight_gets_rows_built_from_the_completed_history(world):
    small_world(world)
    upcoming = world.fight(1, 2, date(2030, 3, 1), status="scheduled")

    report = world.build(today=date(2030, 1, 1))
    rows = world.features()

    assert report["fights_upcoming"] == 1
    assert rows[(upcoming, 1)]["prior_ufc_fights"] == 2
    assert rows[(upcoming, 2)]["prior_ufc_fights"] == 2


def test_a_scheduled_fight_dated_today_still_counts_as_upcoming(world):
    small_world(world)
    tonight = world.fight(1, 2, date(2030, 1, 1), status="scheduled")

    world.build(today=date(2030, 1, 1))

    assert (tonight, 1) in world.features()


def test_a_past_dated_scheduled_fight_is_excluded_and_reported(world):
    small_world(world)
    stale = world.fight(1, 2, date(2029, 6, 1), status="scheduled")

    report = world.build(today=date(2030, 1, 1))

    assert report["excluded_past_dated_scheduled"] == {
        "count": 1,
        "fights": [{"fight_id": stale, "event_date": "2029-06-01"}],
    }
    assert not [k for k in world.features() if k[0] == stale]
    assert report["fights_upcoming"] == 0


def test_a_fighter_with_an_unresolved_past_bout_is_flagged_on_later_rows_only(world):
    for fighter_id in (1, 2, 3, 4):
        world.fighter(fighter_id)
    before_gap = world.fight(1, 2, date(2029, 1, 1), winner=1, stats={1: ROW, 2: ROW})
    world.fight(1, 3, date(2029, 6, 1), status="scheduled")  # happened, but no result ever came
    after_gap = world.fight(1, 4, date(2029, 9, 1), winner=1, stats={1: ROW, 4: ROW})
    upcoming = world.fight(1, 2, date(2030, 3, 1), status="scheduled")

    world.build(today=date(2030, 1, 1))
    rows = world.features()

    assert rows[(before_gap, 1)]["has_unresolved_prior_fight"] is False  # the gap comes later
    assert rows[(after_gap, 1)]["has_unresolved_prior_fight"] is True
    assert rows[(after_gap, 4)]["has_unresolved_prior_fight"] is False  # fighter 4 has no gap
    assert rows[(upcoming, 1)]["has_unresolved_prior_fight"] is True
    assert rows[(upcoming, 2)]["has_unresolved_prior_fight"] is False


def test_a_future_scheduled_fight_is_not_an_unresolved_gap(world):
    for fighter_id in (1, 2, 3):
        world.fighter(fighter_id)
    world.fight(1, 2, date(2029, 1, 1), winner=1, stats={1: ROW, 2: ROW})
    soon = world.fight(1, 2, date(2030, 2, 1), status="scheduled")
    later = world.fight(1, 3, date(2030, 4, 1), status="scheduled")

    world.build(today=date(2030, 1, 1))
    rows = world.features()

    assert rows[(soon, 1)]["has_unresolved_prior_fight"] is False
    assert rows[(later, 1)]["has_unresolved_prior_fight"] is False  # `soon` hasn't happened yet


def test_cancelled_fights_are_not_built_and_not_history(world):
    small_world(world)
    cancelled = world.fight(1, 3, date(2022, 3, 1), status="cancelled")
    after = world.fight(1, 2, date(2022, 6, 1), winner=1, stats={1: ROW, 2: ROW})

    world.build()
    rows = world.features()

    assert not [k for k in rows if k[0] == cancelled]
    assert rows[(after, 1)]["prior_ufc_fights"] == 2
    assert rows[(after, 1)]["has_unresolved_prior_fight"] is False


# -- bio facts ---------------------------------------------------------------------------------


def test_bio_fields_and_age_come_through(world):
    world.fighter(1, dob=date(1990, 1, 1), height_cm=180.5, reach_cm=185.0, stance="Southpaw")
    world.fighter(2)
    fight = world.fight(1, 2, date(2020, 1, 1) + timedelta(days=10), winner=1)

    world.build()
    row = world.features()[(fight, 1)]

    assert (row["height_cm"], row["reach_cm"], row["stance"]) == (180.5, 185.0, "Southpaw")
    assert row["age_days"] == (date(2020, 1, 11) - date(1990, 1, 1)).days
    assert world.features()[(fight, 2)]["age_days"] is None
    assert row["weight_class"] == "Lightweight"
