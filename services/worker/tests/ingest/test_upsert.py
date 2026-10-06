from concurrent.futures import ThreadPoolExecutor

import pytest
import sqlalchemy as sa
from sqlalchemy import Boolean, Column, MetaData, Table, Text, text

from cageops_common.db.models import Fighter
from cageops_worker.ingest.upsert import (
    UpsertCounts,
    insert_missing,
    read_counts,
    record_counts,
    upsert,
)

# A scratch table in the test database (dropped with the connection) so the policies are tested on
# their own, apart from the real schema.
SCRATCH = Table(
    "scratch_upsert",
    MetaData(),
    Column("k", Text, primary_key=True),
    Column("a", Text),  # replaced
    Column("b", Text),  # filled
    Column("flag", Boolean),  # sticky true
    Column("tag", Text),
    prefixes=["TEMPORARY"],
)
POLICY = dict(key=["k"], replace=["a"], fill=["b"], sticky_true=["flag"])


@pytest.fixture
def conn(db):
    with db.connect() as connection:
        SCRATCH.create(connection)
        yield connection
        connection.rollback()


def row(**kwargs):
    return {"k": "x", "a": None, "b": None, "flag": False} | kwargs


def fetch(conn):
    return {r.k: r for r in conn.execute(sa.select(SCRATCH))}


def xmin(conn, k="x"):
    return conn.execute(
        text("SELECT xmin::text FROM scratch_upsert WHERE k = :k"), {"k": k}
    ).scalar()


# -- counts ----------------------------------------------------------------------------


def test_new_rows_are_counted_as_inserted(conn):
    counts = upsert(conn, SCRATCH, [row(k="1"), row(k="2")], **POLICY)

    assert counts == UpsertCounts(inserted=2, updated=0, unchanged=0)


def test_an_identical_rerun_writes_nothing_at_all(conn):
    upsert(conn, SCRATCH, [row(a="A", b="B", flag=True)], **POLICY)
    before = xmin(conn)  # the id of the transaction that wrote the row

    counts = upsert(conn, SCRATCH, [row(a="A", b="B", flag=True)], **POLICY)

    assert counts == UpsertCounts(inserted=0, updated=0, unchanged=1)
    assert xmin(conn) == before  # not even a new row version


def test_a_changed_value_is_counted_as_updated_and_the_rest_as_unchanged(conn):
    upsert(conn, SCRATCH, [row(k="1", a="old"), row(k="2", a="same")], **POLICY)

    counts = upsert(conn, SCRATCH, [row(k="1", a="new"), row(k="2", a="same")], **POLICY)

    assert counts == UpsertCounts(inserted=0, updated=1, unchanged=1)
    assert fetch(conn)["1"].a == "new"


def test_one_call_can_insert_update_and_leave_alone(conn):
    upsert(conn, SCRATCH, [row(k="old", a="x"), row(k="same", a="s")], **POLICY)

    counts = upsert(
        conn, SCRATCH, [row(k="new", a="n"), row(k="old", a="y"), row(k="same", a="s")], **POLICY
    )

    assert (counts.inserted, counts.updated, counts.unchanged) == (1, 1, 1)
    assert counts.total == 3


def test_nothing_to_write_is_zero_counts(conn):
    assert upsert(conn, SCRATCH, [], **POLICY) == UpsertCounts()


def test_duplicate_keys_in_one_call_are_an_error(conn):
    with pytest.raises(ValueError, match="duplicate keys"):
        upsert(conn, SCRATCH, [row(a="1"), row(a="2")], **POLICY)


# -- the three column policies ---------------------------------------------------------


def test_replace_columns_take_the_new_value_even_when_it_is_null(conn):
    """A result that was overturned must be able to clear the old winner."""
    upsert(conn, SCRATCH, [row(a="winner")], **POLICY)

    counts = upsert(conn, SCRATCH, [row(a=None)], **POLICY)

    assert fetch(conn)["x"].a is None
    assert counts.updated == 1


def test_fill_columns_never_lose_a_value_to_a_null(conn):
    upsert(conn, SCRATCH, [row(b="known")], **POLICY)

    counts = upsert(conn, SCRATCH, [row(b=None)], **POLICY)

    assert fetch(conn)["x"].b == "known"
    assert counts.unchanged == 1  # a blank page changed nothing


def test_fill_columns_fill_a_null_and_overwrite_a_value(conn):
    upsert(conn, SCRATCH, [row(b=None)], **POLICY)
    assert upsert(conn, SCRATCH, [row(b="filled")], **POLICY).updated == 1
    assert fetch(conn)["x"].b == "filled"

    assert upsert(conn, SCRATCH, [row(b="corrected")], **POLICY).updated == 1
    assert fetch(conn)["x"].b == "corrected"


def test_a_sticky_boolean_goes_up_but_never_back_down(conn):
    upsert(conn, SCRATCH, [row(flag=False)], **POLICY)
    assert upsert(conn, SCRATCH, [row(flag=True)], **POLICY).updated == 1
    assert fetch(conn)["x"].flag is True

    counts = upsert(conn, SCRATCH, [row(flag=False)], **POLICY)

    assert fetch(conn)["x"].flag is True
    assert counts.unchanged == 1


def test_columns_outside_the_policy_are_never_touched(conn):
    upsert(conn, SCRATCH, [row(a="1")], **POLICY)
    conn.execute(text("UPDATE scratch_upsert SET tag = 'mine'"))

    upsert(conn, SCRATCH, [row(a="2")], **POLICY)

    assert fetch(conn)["x"].tag == "mine"


def test_only_if_blocks_updates_to_rows_that_fail_the_condition(conn):
    upsert(conn, SCRATCH, [row(a="protected")], **POLICY)
    conn.execute(text("UPDATE scratch_upsert SET tag = 'locked'"))

    counts = upsert(
        conn,
        SCRATCH,
        [row(a="attempt")],
        **POLICY,
        only_if=SCRATCH.c.tag.is_distinct_from("locked"),
    )

    assert fetch(conn)["x"].a == "protected"
    assert counts.unchanged == 1


def test_a_table_with_nothing_to_update_just_inserts_once(conn):
    first = upsert(conn, SCRATCH, [row()], key=["k"])
    again = upsert(conn, SCRATCH, [row()], key=["k"])

    assert (first.inserted, again.unchanged) == (1, 1)


# -- the real tables, and races --------------------------------------------------------


def fighter(i, **kwargs):
    return {"ufcstats_id": f"{i:016x}", "name": f"Fighter {i}", "dob": None, "height_cm": None,
            "reach_cm": None, "stance": None} | kwargs  # fmt: skip


FIGHTER_POLICY = dict(
    key=["ufcstats_id"], replace=["name"], fill=["dob", "height_cm", "reach_cm", "stance"]
)


def test_eight_workers_upserting_the_same_rows_insert_each_row_exactly_once(db):
    rows = [fighter(i) for i in range(50)]

    def work(_):
        with db.begin() as connection:
            return upsert(connection, Fighter.__table__, rows, **FIGHTER_POLICY)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(work, range(8)))

    assert sum(r.inserted for r in results) == 50  # no row inserted twice, none lost
    assert sum(r.total for r in results) == 8 * 50
    with db.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM fighters")).scalar_one() == 50


def test_insert_missing_leaves_existing_rows_alone(db):
    with db.begin() as connection:
        upsert(connection, Fighter.__table__, [fighter(1, stance="Southpaw")], **FIGHTER_POLICY)

        counts = insert_missing(
            connection,
            Fighter.__table__,
            [{"ufcstats_id": fighter(1)["ufcstats_id"], "name": "Stub"},
             {"ufcstats_id": fighter(2)["ufcstats_id"], "name": "New"}],
            key=["ufcstats_id"],
        )  # fmt: skip
        names = dict(connection.execute(text("SELECT ufcstats_id, name FROM fighters")).all())
        stance = connection.execute(
            text("SELECT stance FROM fighters WHERE name = 'Fighter 1'")
        ).scalar()

    assert counts == UpsertCounts(inserted=1, updated=0, unchanged=1)
    assert names[fighter(1)["ufcstats_id"]] == "Fighter 1"  # not renamed to "Stub"
    assert stance == "Southpaw"


# -- per-run totals --------------------------------------------------------------------


def test_run_totals_add_up_across_jobs_and_workers(redis_client):
    record_counts(redis_client, "run-1", "fights", UpsertCounts(inserted=3, updated=1, unchanged=0))
    record_counts(redis_client, "run-1", "fights", UpsertCounts(inserted=2, updated=0, unchanged=5))
    record_counts(redis_client, "run-1", "events", UpsertCounts(inserted=1))

    assert read_counts(redis_client, "run-1") == {
        "fights:inserted": 5,
        "fights:updated": 1,
        "fights:unchanged": 5,
        "events:inserted": 1,
    }
    assert read_counts(redis_client, "run-2") == {}
    assert 0 < redis_client.ttl("ingest:run:run-1") <= 7 * 24 * 3600
