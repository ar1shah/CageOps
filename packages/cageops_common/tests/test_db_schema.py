from datetime import date

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from cageops_common.db.models import Base

EXPECTED_TABLES = {
    "events",
    "fight_round_stats",
    "fight_totals",
    "fighter_aliases",
    "fighters",
    "fights",
    "load_runs",
    "odds",
    "rankings",
    "raw_pages",
}


def seed_fight(conn, a=1, b=2, **overrides):
    """Insert two fighters, an event and one fight; returns the fight id."""
    conn.execute(
        text(
            "INSERT INTO fighters (id, ufcstats_id, name) VALUES (:a, 'fa', 'A'), (:b, 'fb', 'B')"
        ),
        {"a": a, "b": b},
    )
    conn.execute(
        text("INSERT INTO events (id, ufcstats_id, name, event_date) VALUES (1, 'e1', 'E', :d)"),
        {"d": date(2019, 10, 5)},
    )
    row = {
        "a": a,
        "b": b,
        "winner": a,
        "outcome": "win",
        "method": "ko_tko",
        "red": None,
    } | overrides
    return conn.execute(
        text(
            "INSERT INTO fights (ufcstats_id, event_id, fighter_a_id, fighter_b_id, red_fighter_id,"
            " weight_class, gender, is_title_fight, outcome, winner_id, method, has_round_stats)"
            " VALUES ('f1', 1, :a, :b, :red, 'Middleweight', 'M', true, :outcome, :winner,"
            " :method, true) RETURNING id"
        ),
        row,
    ).scalar_one()


def test_migrations_create_every_table_and_vector_extension(db):
    with db.connect() as conn:
        tables = set(
            conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            ).scalars()
        )
        extensions = set(conn.execute(text("SELECT extname FROM pg_extension")).scalars())

    assert tables >= EXPECTED_TABLES
    assert "vector" in extensions


def test_models_and_migrations_have_not_drifted(db):
    with db.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)

    assert diff == []


def test_fight_requires_neutral_fighter_order(db):
    with pytest.raises(IntegrityError, match="neutral_fighter_order"), db.begin() as conn:
        seed_fight(conn, a=2, b=1)


def test_win_requires_a_winner_and_non_win_must_not_have_one(db):
    with pytest.raises(IntegrityError, match="win_has_winner"), db.begin() as conn:
        seed_fight(conn, outcome="win", winner=None)


def test_winner_must_be_a_participant(db):
    with pytest.raises(IntegrityError, match="winner_is_a_participant"), db.begin() as conn:
        conn.execute(text("INSERT INTO fighters (id, ufcstats_id, name) VALUES (3, 'fc', 'C')"))
        seed_fight(conn, winner=3)


def test_odds_source_is_restricted(db):
    with db.begin() as conn:
        fight_id = seed_fight(conn)
    with pytest.raises(IntegrityError, match="odds_source_values"), db.begin() as conn:
        conn.execute(
            text("INSERT INTO odds (fight_id, source) VALUES (:f, 'somewhere_else')"),
            {"f": fight_id},
        )


def test_odds_captured_at_can_be_unknown(db):
    with db.begin() as conn:
        fight_id = seed_fight(conn)
        conn.execute(
            text(
                "INSERT INTO odds (fight_id, source, decimal_odds_a, decimal_odds_b)"
                " VALUES (:f, 'silver', 1.5, 2.8)"
            ),
            {"f": fight_id},
        )
        captured = conn.execute(text("SELECT captured_at FROM odds")).scalar_one()

    assert captured is None


def test_migrations_reverse_to_base_and_apply_again_on_an_empty_database(db, alembic_config):
    """Every downgrade works when the tables are empty. (With data loaded, downgrading past
    0002 fails on purpose: fights with a NULL weight_class can't go back to NOT NULL.)"""
    with db.begin() as conn:
        alembic_config.attributes["connection"] = conn
        command.downgrade(alembic_config, "base")
    with db.connect() as conn:
        left = set(
            conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            ).scalars()
        )
        views = conn.execute(
            text("SELECT count(*) FROM pg_views WHERE schemaname = 'public'")
        ).scalar_one()
    assert left == {"alembic_version"} and views == 0

    with db.begin() as conn:
        alembic_config.attributes["connection"] = conn
        command.upgrade(alembic_config, "head")
    with db.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


# -- fights.status lifecycle (D-023) -----------------------------------------------------------


def insert_bout(conn, **overrides):
    """Insert a fight with explicit control over every result column; returns its id.
    Defaults to a scheduled bout (no result at all)."""
    conn.execute(
        text(
            "INSERT INTO fighters (id, ufcstats_id, name) VALUES (1, 'fa', 'A'), (2, 'fb', 'B')"
            " ON CONFLICT DO NOTHING"
        )
    )
    conn.execute(
        text(
            "INSERT INTO events (id, ufcstats_id, name, event_date) VALUES (1, 'e1', 'E', :d)"
            " ON CONFLICT DO NOTHING"
        ),
        {"d": date(2026, 10, 10)},
    )
    row = {
        "uid": "bout1",
        "status": "scheduled",
        "outcome": None,
        "winner": None,
        "method": None,
        "title": None,
        "stats": False,
    } | overrides
    return conn.execute(
        text(
            "INSERT INTO fights (ufcstats_id, event_id, fighter_a_id, fighter_b_id, weight_class,"
            " gender, is_title_fight, outcome, winner_id, method, has_round_stats, status)"
            " VALUES (:uid, 1, 1, 2, 'Middleweight', 'M', :title, :outcome, :winner, :method,"
            " :stats, :status) RETURNING id"
        ),
        row,
    ).scalar_one()


COMPLETED = {
    "status": "completed",
    "outcome": "win",
    "winner": 1,
    "method": "decision",
    "title": False,
}


def test_existing_style_inserts_default_to_completed(db):
    with db.begin() as conn:
        seed_fight(conn)
        status = conn.execute(text("SELECT status FROM fights")).scalar_one()

    assert status == "completed"


@pytest.mark.parametrize("status", ["scheduled", "cancelled"])
def test_an_unfought_bout_has_null_outcome_method_and_title_flag(db, status):
    with db.begin() as conn:
        insert_bout(conn, status=status)
        row = conn.execute(text("SELECT outcome, method, is_title_fight FROM fights")).one()

    assert tuple(row) == (None, None, None)


def test_a_scheduled_bout_cannot_have_a_winner(db):
    """Two constraints each reject this row (win_has_winner and unfought_has_no_result); which
    one Postgres reports first doesn't matter, only that the row can't exist."""
    with (
        pytest.raises(IntegrityError, match="win_has_winner|unfought_has_no_result"),
        db.begin() as conn,
    ):
        insert_bout(conn, winner=1)


def test_the_old_win_has_winner_expression_was_null_for_an_unfought_bout(db):
    """Why win_has_winner was rewritten: a CHECK passes when its condition is NULL. For a bout with
    no outcome the old expression is NULL (so it would let a winner through); the new one is a
    definite false."""
    with db.connect() as conn:
        old = conn.execute(
            text("SELECT (NULL::text = 'win') = (1 IS NOT NULL)")  # outcome NULL, winner_id set
        ).scalar_one()
        new = conn.execute(
            text("SELECT (NULL::text IS NOT DISTINCT FROM 'win') = (1 IS NOT NULL)")
        ).scalar_one()

    assert old is None  # NULL: a CHECK would accept it
    assert new is False  # a CHECK would reject it


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"outcome": "unknown"}, "unfought_has_no_result"),  # 'unknown' is for completed fights
        ({"method": "decision"}, "unfought_has_no_result"),
        ({"stats": True}, "unfought_has_no_result"),
        ({"status": "cancelled", "method": "ko_tko"}, "unfought_has_no_result"),
        ({"status": "postponed"}, "status_values"),
    ],
)
def test_an_unfought_bout_with_any_result_data_is_rejected(db, overrides, constraint):
    with pytest.raises(IntegrityError, match=constraint), db.begin() as conn:
        insert_bout(conn, **overrides)


@pytest.mark.parametrize("missing", ["outcome", "method", "title"])
def test_a_completed_fight_needs_an_outcome_a_method_and_a_title_flag(db, missing):
    overrides = COMPLETED | {missing: None}
    if missing == "outcome":
        overrides["winner"] = None  # keep win_has_winner satisfied so the right check fires

    with pytest.raises(IntegrityError, match="completed_has_result"), db.begin() as conn:
        insert_bout(conn, **overrides)


def test_unknown_still_means_a_completed_fight_with_an_undetermined_result(db):
    with db.begin() as conn:
        insert_bout(conn, **(COMPLETED | {"outcome": "unknown", "winner": None}))
        assert conn.execute(text("SELECT outcome FROM fights")).scalar_one() == "unknown"


def test_a_scheduled_bout_becomes_completed_when_its_result_arrives(db):
    with db.begin() as conn:
        fight_id = insert_bout(conn)
        conn.execute(
            text(
                "UPDATE fights SET status = 'completed', outcome = 'win', winner_id = 1,"
                " method = 'decision', is_title_fight = false, has_round_stats = true"
                " WHERE id = :i"
            ),
            {"i": fight_id},
        )
        assert conn.execute(text("SELECT status FROM fights")).scalar_one() == "completed"


def test_completed_fights_view_only_has_finished_fights(db):
    with db.begin() as conn:
        insert_bout(conn, uid="played", **COMPLETED)
        insert_bout(conn, uid="upcoming")
        insert_bout(conn, uid="removed", status="cancelled")
        visible = conn.execute(text("SELECT ufcstats_id FROM completed_fights")).scalars().all()

    assert visible == ["played"]


def test_the_rankings_view_also_covers_scheduled_bouts(db):
    """Phase 2b needs ranks for upcoming fights, so fight_pre_rankings must include them."""
    with db.begin() as conn:
        insert_bout(conn)
        rows = conn.execute(text("SELECT fighter_id, status FROM fight_pre_rankings")).all()

    assert {r[0] for r in rows} == {1, 2}


def test_downgrade_refuses_while_unfought_bouts_exist_and_changes_nothing(db, alembic_config):
    with db.begin() as conn:
        insert_bout(conn)

    with pytest.raises(RuntimeError, match="1 scheduled or cancelled"), db.begin() as conn:
        alembic_config.attributes["connection"] = conn
        command.downgrade(alembic_config, "0005")

    with db.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM fights")).scalar_one() == 1
        columns = set(
            conn.execute(
                text("SELECT column_name FROM information_schema.columns WHERE table_name='fights'")
            ).scalars()
        )
    assert "status" in columns
