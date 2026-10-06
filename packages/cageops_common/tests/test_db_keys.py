"""Per-source keys, fights.result_source and the fight natural key (migration 0008, D-029)."""

from datetime import date

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

HEX_A, HEX_B, HEX_C, HEX_E = (f"{c}" * 4 + "000000000001" for c in "abce")


def run(db, sql, **params):
    with db.begin() as conn:
        return conn.execute(text(sql), params).scalar()


def fighter(db, ufcstats_id=None, title=None, name="F"):
    return run(
        db,
        "INSERT INTO fighters (ufcstats_id, wikipedia_title, name) VALUES (:u, :t, :n)"
        " RETURNING id",
        u=ufcstats_id,
        t=title,
        n=name,
    )


def event(db, ufcstats_id=HEX_E, article=None, day=date(2026, 6, 6)):
    return run(
        db,
        "INSERT INTO events (ufcstats_id, wikipedia_article_id, name, event_date)"
        " VALUES (:u, :a, 'E', :d) RETURNING id",
        u=ufcstats_id,
        a=article,
        d=day,
    )


def fight(db, event_id, a, b, uid=None, status="completed", source="ufcstats"):
    done = status == "completed"
    return run(
        db,
        "INSERT INTO fights (ufcstats_id, event_id, fighter_a_id, fighter_b_id, gender, status,"
        " is_title_fight, outcome, method, has_round_stats, result_source)"
        " VALUES (:u, :e, :a, :b, 'M', :s, :t, :o, :m, false, :src) RETURNING id",
        u=uid,
        e=event_id,
        a=min(a, b),
        b=max(a, b),
        s=status,
        t=False if done else None,
        o="draw" if done else None,
        m="decision" if done else None,
        src=source,
    )


# -- ufcstats_id holds only real ufcstats ids ------------------------------------------------


@pytest.mark.parametrize("bad", ["wp:1234567890abcdef", "short", "ABCDEF0123456789", "g" * 16])
def test_a_made_up_id_cannot_go_in_ufcstats_id(db, bad):
    with pytest.raises(IntegrityError, match="ufcstats_id_format"):
        fighter(db, ufcstats_id=bad)
    with pytest.raises(IntegrityError, match="ufcstats_id_format"):
        event(db, ufcstats_id=bad, article=1)


def test_a_fight_another_source_created_has_no_ufcstats_id(db):
    a, b = fighter(db, title="A"), fighter(db, title="B")
    e = event(db, ufcstats_id=None, article=7)

    fight_id = fight(db, e, a, b, uid=None, source="wikipedia")

    assert run(db, "SELECT ufcstats_id FROM fights WHERE id = :i", i=fight_id) is None


def test_several_rows_may_lack_a_ufcstats_id(db):
    fighter(db, title="A")
    fighter(db, title="B")
    event(db, ufcstats_id=None, article=1)
    event(db, ufcstats_id=None, article=2)
    assert run(db, "SELECT count(*) FROM fighters WHERE ufcstats_id IS NULL") == 2


# -- an event or fighter needs at least one key; the other-source keys are unique ------------------


def test_an_event_and_a_fighter_need_some_key(db):
    with pytest.raises(IntegrityError, match="has_a_source_key"):
        event(db, ufcstats_id=None, article=None)
    with pytest.raises(IntegrityError, match="has_a_source_key"):
        fighter(db, ufcstats_id=None, title=None)


def test_the_wikipedia_keys_are_unique(db):
    event(db, ufcstats_id=None, article=99)
    fighter(db, title="Zhang_Weili")
    with pytest.raises(IntegrityError, match="wikipedia_article_id"):
        event(db, ufcstats_id=None, article=99)
    with pytest.raises(IntegrityError, match="wikipedia_title"):
        fighter(db, title="Zhang_Weili")


def test_one_row_can_carry_both_keys(db):
    event(db, ufcstats_id=HEX_E, article=5)
    fighter(db, ufcstats_id=HEX_A, title="Some_Fighter")
    assert run(db, "SELECT count(*) FROM events WHERE ufcstats_id IS NOT NULL") == 1


# -- fights.result_source and the natural key ----------------------------------------------------


def test_result_source_is_set_exactly_when_the_fight_has_a_result(db):
    a, b = fighter(db, HEX_A), fighter(db, HEX_B)
    e = event(db)
    with pytest.raises(IntegrityError, match="result_source_iff_completed"):
        fight(db, e, a, b, uid=HEX_C, source=None)  # completed, no source
    with pytest.raises(IntegrityError, match="result_source_iff_completed"):
        fight(db, e, a, b, uid=HEX_C, status="scheduled", source="ufcstats")  # no result, a source

    assert fight(db, e, a, b, uid=HEX_C, status="scheduled", source=None)


def test_result_source_only_allows_the_known_sources(db):
    a, b = fighter(db, HEX_A), fighter(db, HEX_B)
    with pytest.raises(IntegrityError, match="result_source_values"):
        fight(db, event(db), a, b, uid=HEX_C, source="tapology")


def test_one_fight_per_pair_per_event(db):
    a, b, c = fighter(db, HEX_A), fighter(db, HEX_B), fighter(db, HEX_C)
    e1, e2 = event(db), event(db, ufcstats_id="e" * 4 + "000000000002")
    fight(db, e1, a, b, uid="1" * 16)

    with pytest.raises(IntegrityError, match="uq_fights_event_pair"):
        fight(db, e1, b, a, uid="2" * 16)  # same pair, either order, same event
    assert fight(db, e2, a, b, uid="3" * 16)  # a rematch on another card is fine
    assert fight(db, e1, a, c, uid="4" * 16)


# -- the migration -----------------------------------------------------------------------------


def test_existing_completed_fights_become_ufcstats_results(db, alembic_config):
    """Go back to 0007, add a completed fight the old way, come forward: it is a ufcstats result."""
    with db.begin() as conn:
        alembic_config.attributes["connection"] = conn
        command.downgrade(alembic_config, "0007")
        conn.execute(
            text("INSERT INTO fighters (id, ufcstats_id, name) VALUES (1, :a, 'A'), (2, :b, 'B')"),
            {"a": HEX_A, "b": HEX_B},
        )
        conn.execute(
            text("INSERT INTO events (id, ufcstats_id, name, event_date) VALUES (1, :e, 'E', :d)"),
            {"e": HEX_E, "d": date(2020, 1, 1)},
        )
        conn.execute(
            text(
                "INSERT INTO fights (ufcstats_id, event_id, fighter_a_id, fighter_b_id, gender,"
                " is_title_fight, outcome, method, has_round_stats)"
                " VALUES (:u, 1, 1, 2, 'M', false, 'draw', 'decision', false)"
            ),
            {"u": HEX_C},
        )
        command.upgrade(alembic_config, "head")

    assert run(db, "SELECT result_source FROM fights WHERE ufcstats_id = :u", u=HEX_C) == "ufcstats"


def test_downgrade_refuses_while_rows_from_another_source_exist(db, alembic_config):
    fighter(db, title="Only_On_Wikipedia")

    with (
        pytest.raises(RuntimeError, match="1 fighters rows have no ufcstats_id"),
        db.begin() as conn,
    ):
        alembic_config.attributes["connection"] = conn
        command.downgrade(alembic_config, "0007")

    assert run(db, "SELECT count(*) FROM fighters") == 1  # nothing was changed
