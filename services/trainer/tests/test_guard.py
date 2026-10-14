"""When training is refused and when it only warns (D-033).

Refuse: not one complete build, or the inputs the build read have changed. Warn: anything that is
merely a timestamp, because with scheduled scraping there is nearly always a newer fetch."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from cageops_trainer.dataset import StaleFeatures, check_features

REBUILD = "features rebuild"


def check(lab):
    with lab.engine.begin() as conn:
        return check_features(conn)


def refused(lab) -> str:
    with pytest.raises(StaleFeatures) as excinfo:
        check(lab)
    return str(excinfo.value)


def first_labeled(lab):
    return next(f for f in lab.fights if f.status == "completed" and f.outcome == "win")


# -- fresh ----------------------------------------------------------------------------------


def test_a_fresh_build_passes_with_no_warnings(lab):
    build = check(lab)

    assert build.load_run_id == lab.run_id
    assert build.warnings == []
    assert len(build.inputs_sha256) == 64
    assert build.output_sha256 == "c" * 64


# -- refusals -------------------------------------------------------------------------------


def test_an_empty_feature_table_is_refused(lab):
    lab.sql("DELETE FROM fight_features")

    assert REBUILD in refused(lab)


def test_a_labeled_fight_with_a_missing_feature_row_is_refused(lab):
    fight = first_labeled(lab)
    lab.sql(
        "DELETE FROM fight_features WHERE fight_id = :f AND fighter_id = :p",
        f=fight.fight_id,
        p=fight.a,
    )

    message = refused(lab)

    assert "do not have exactly two feature rows" in message and REBUILD in message


def test_rows_from_two_different_builds_are_refused(lab):
    fight = first_labeled(lab)
    with lab.engine.begin() as conn:
        second = conn.execute(
            text(
                "INSERT INTO load_runs (source, file_path, sha256)"
                " VALUES ('features', 'db:completed_fights', repeat('d', 64)) RETURNING id"
            )
        ).scalar_one()
        conn.execute(
            text("UPDATE fight_features SET load_run_id = :r WHERE fight_id = :f"),
            {"r": second, "f": fight.fight_id},
        )

    message = refused(lab)

    assert "different builds" in message and REBUILD in message


def test_a_result_changed_in_place_with_no_timestamp_moving_is_refused(lab):
    """The case only the fingerprint can see: an overturn or a DLQ replay from cached HTML."""
    fight = first_labeled(lab)
    loser = fight.b if fight.winner == fight.a else fight.a
    lab.sql("UPDATE fights SET winner_id = :w WHERE id = :f", w=loser, f=fight.fight_id)

    message = refused(lab)

    assert "have changed since the build" in message
    assert REBUILD in message


def test_a_changed_bio_is_refused(lab):
    lab.sql("UPDATE fighters SET reach_cm = reach_cm + 5 WHERE id = 1")

    assert REBUILD in refused(lab)


def test_a_build_with_no_stored_fingerprint_is_refused_and_says_why(lab):
    lab.sql("UPDATE load_runs SET report = '{}'::jsonb WHERE id = :i", i=lab.run_id)

    message = refused(lab)

    assert "before the input fingerprint existed" in message and REBUILD in message


def test_the_refusal_names_a_newer_load_as_the_likely_cause(lab):
    fight = first_labeled(lab)
    lab.sql("UPDATE fights SET weight_class = 'Flyweight' WHERE id = :f", f=fight.fight_id)
    lab.sql(
        "INSERT INTO load_runs (source, file_path, sha256, finished_at)"
        " VALUES ('silver', 'x.csv', repeat('0', 64), now())"
    )

    message = refused(lab)

    assert "Newer loads were seen" in message and "silver" in message


# -- warnings: still trains ------------------------------------------------------------------


def test_a_newer_seed_load_only_warns(lab):
    lab.sql(
        "INSERT INTO load_runs (source, file_path, sha256, finished_at)"
        " VALUES ('mdabbert', 'ufc-master.csv', repeat('0', 64), now())"
    )

    build = check(lab)

    assert any("mdabbert" in w and "after the features build" in w for w in build.warnings)


def test_an_unfinished_load_warns_and_says_unfinished(lab):
    lab.sql(
        "INSERT INTO load_runs (source, file_path, sha256)"
        " VALUES ('silver', 'x.csv', repeat('0', 64))"
    )

    build = check(lab)

    assert any("silver" in w and "unfinished" in w for w in build.warnings)


def test_a_newer_fetched_page_only_warns(lab):
    lab.sql(
        "INSERT INTO raw_pages (url, source, fetched_at, status, html)"
        " VALUES ('http://example.test/e', 'wikipedia', now(), 200, '<html/>')"
    )

    build = check(lab)

    assert any("raw page" in w and "fetched after the features build" in w for w in build.warnings)


def test_an_older_fetch_is_not_a_warning(lab):
    lab.sql(
        "INSERT INTO raw_pages (url, source, fetched_at, status, html)"
        " VALUES ('http://example.test/e', 'wikipedia', now() - interval '1 day', 200, '<html/>')"
    )

    assert check(lab).warnings == []


def test_a_scheduled_bout_changing_does_not_refuse_or_warn(lab):
    upcoming = next(f for f in lab.fights if f.status == "scheduled")
    lab.sql("UPDATE fights SET weight_class = 'Flyweight' WHERE id = :f", f=upcoming.fight_id)
    lab.sql("UPDATE fight_features SET reach_cm = 200 WHERE fight_id = :f", f=upcoming.fight_id)

    assert check(lab).warnings == []
