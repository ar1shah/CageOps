from datetime import date

import pytest
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
