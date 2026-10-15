"""The builder stores a fingerprint of what it read (D-033), and that fingerprint covers
everything it reads.

The trainer refuses to train when the stored value no longer matches the database, so these tests
pin the two promises it depends on: the stored value is the real one, and no column the builder
reads can change without the value changing.
"""

from datetime import date

from sqlalchemy import text

from cageops_common.db.fingerprint import INCLUDED, features_input_fingerprint
from cageops_worker.features import history

from .test_build import small_world


def current_fingerprint(world) -> str:
    with world.engine.connect() as conn:
        return features_input_fingerprint(conn)


def test_the_report_and_the_load_run_carry_the_fingerprint_of_what_was_read(world):
    small_world(world)

    report = world.build()

    assert report["inputs_sha256"] == current_fingerprint(world)
    with world.engine.connect() as conn:
        stored = conn.execute(
            text("SELECT report->>'inputs_sha256' FROM load_runs WHERE source = 'features'")
        ).scalar_one()
    assert stored == report["inputs_sha256"]
    assert report["inputs_sha256"] != report["output_sha256"]


def test_rebuilding_unchanged_data_gives_the_same_inputs_fingerprint(world):
    small_world(world)

    first = world.build()
    second = world.build()

    assert first["inputs_sha256"] == second["inputs_sha256"]


def test_changing_a_result_changes_the_inputs_fingerprint(world):
    f1, *_ = small_world(world)
    before = world.build()["inputs_sha256"]

    # An overturn: the win becomes a no contest. The old features are now stale.
    world.sql("UPDATE fights SET outcome = 'no_contest', winner_id = NULL WHERE id = :f", f=f1)

    assert current_fingerprint(world) != before
    assert world.build()["inputs_sha256"] != before


def test_a_scheduled_bout_changes_the_features_but_not_the_inputs_fingerprint(world):
    small_world(world)
    before = world.build(today=date(2022, 1, 1))

    world.fight(1, 2, date(2022, 6, 1), status="scheduled")
    after = world.build(today=date(2022, 1, 1))

    assert after["fights_upcoming"] == 1 and before["fights_upcoming"] == 0
    assert after["output_sha256"] != before["output_sha256"]  # new rows for the upcoming bout
    assert after["inputs_sha256"] == before["inputs_sha256"]  # nothing that trains changed


def test_everything_the_builder_reads_is_in_the_fingerprint():
    assert set(history.FIGHT_COLUMNS) <= set(INCLUDED["fights"])
    assert set(history.EVENT_COLUMNS) <= set(INCLUDED["events"])
    assert set(history.FIGHTER_COLUMNS) <= set(INCLUDED["fighters"])
    assert set(history.TOTALS_COLUMNS) <= set(INCLUDED["fight_totals"])


def test_the_fingerprint_hashes_nothing_the_builder_does_not_read():
    # event_id is the one extra: it is how a fight moved between events is seen.
    assert set(INCLUDED["fights"]) == set(history.FIGHT_COLUMNS) | {"event_id"}
    assert set(INCLUDED["events"]) == set(history.EVENT_COLUMNS)
    assert set(INCLUDED["fighters"]) == set(history.FIGHTER_COLUMNS)
    assert set(INCLUDED["fight_totals"]) == set(history.TOTALS_COLUMNS)
