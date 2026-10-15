"""The model registry table (migration 0009, D-033): the database, not the application, is what
guarantees at most one active version."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


@pytest.fixture
def load_run_id(db):
    with db.begin() as conn:
        return conn.execute(
            text(
                "INSERT INTO load_runs (source, file_path, sha256)"
                " VALUES ('features', 'db:completed_fights', repeat('a', 64)) RETURNING id"
            )
        ).scalar_one()


def add(db, load_run_id, version, *, active=False):
    with db.begin() as conn:
        return conn.execute(
            text(
                "INSERT INTO model_versions (version, trained_at, data_cutoff,"
                " features_output_sha256, features_load_run_id, metrics, config, artifact_path,"
                " is_active) VALUES (:v, now(), '2025-01-01', repeat('a', 64), :r, '{}', '{}',"
                " '/m/' || :v, :a) RETURNING id"
            ),
            {"v": version, "r": load_run_id, "a": active},
        ).scalar_one()


def active_versions(db) -> list[str]:
    with db.connect() as conn:
        return list(
            conn.execute(
                text("SELECT version FROM model_versions WHERE is_active ORDER BY version")
            ).scalars()
        )


def test_a_version_is_registered_inactive_by_default(db, load_run_id):
    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO model_versions (version, trained_at, data_cutoff,"
                " features_output_sha256, features_load_run_id, metrics, config, artifact_path)"
                " VALUES ('v1', now(), '2025-01-01', repeat('a', 64), :r, '{}', '{}', '/m/v1')"
            ),
            {"r": load_run_id},
        )

    assert active_versions(db) == []


def test_many_inactive_versions_are_allowed(db, load_run_id):
    for name in ("v1", "v2", "v3"):
        add(db, load_run_id, name)

    assert active_versions(db) == []


def test_the_database_rejects_a_second_active_version(db, load_run_id):
    add(db, load_run_id, "v1", active=True)

    with pytest.raises(IntegrityError, match="uq_model_versions_one_active"):
        add(db, load_run_id, "v2", active=True)

    assert active_versions(db) == ["v1"]


def test_activating_a_second_version_by_update_is_also_rejected(db, load_run_id):
    add(db, load_run_id, "v1", active=True)
    second = add(db, load_run_id, "v2")

    with pytest.raises(IntegrityError, match="uq_model_versions_one_active"), db.begin() as conn:
        conn.execute(
            text("UPDATE model_versions SET is_active = true WHERE id = :i"), {"i": second}
        )

    assert active_versions(db) == ["v1"]


def test_deactivating_then_activating_in_one_transaction_switches_cleanly(db, load_run_id):
    add(db, load_run_id, "v1", active=True)
    second = add(db, load_run_id, "v2")

    with db.begin() as conn:
        conn.execute(text("UPDATE model_versions SET is_active = false WHERE is_active"))
        conn.execute(
            text("UPDATE model_versions SET is_active = true WHERE id = :i"), {"i": second}
        )

    assert active_versions(db) == ["v2"]


def test_version_names_are_unique(db, load_run_id):
    add(db, load_run_id, "v1")

    with pytest.raises(IntegrityError):
        add(db, load_run_id, "v1")
