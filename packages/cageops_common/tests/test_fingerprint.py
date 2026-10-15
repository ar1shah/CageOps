"""The features input fingerprint (D-033): what changes it, what must not, and that every column
of the four tables has been classified.

The hash exists so training can tell "the data the features were built from has changed" from
"something else happened". So the tests pin both sides: every column that feeds a feature or a
label moves it, and bookkeeping, identity and display columns, plus rows that can't reach a
training row, leave it alone.
"""

import pytest
from sqlalchemy import text

from cageops_common.db.fingerprint import (
    EXCLUDED,
    EXCLUDED_TABLES,
    INCLUDED,
    features_input_fingerprint,
)
from cageops_common.db.models import Base

HEX = "abcdef0123456789"
OTHER_HEX = "0123456789abcdef"


def make_world(db) -> dict[str, int]:
    """Fighters 1-4, a completed fight (2 beat 3) with totals for both, and an unrelated empty
    event. Returns the ids the mutations use."""
    with db.begin() as conn:
        for fighter_id in (1, 2, 3, 4):
            conn.execute(
                text(
                    "INSERT INTO fighters (id, ufcstats_id, name, dob, height_cm, reach_cm,"
                    " stance) VALUES (:i, :u, :n, '1990-01-01', 180, 185, 'Orthodox')"
                ),
                {"i": fighter_id, "u": f"{fighter_id:016x}", "n": f"Fighter {fighter_id}"},
            )
        event_id = conn.execute(
            text(
                "INSERT INTO events (ufcstats_id, name, event_date, city, state, country)"
                " VALUES (:u, 'Event', '2024-01-01', 'Orlando', 'Florida', 'USA') RETURNING id"
            ),
            {"u": HEX},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO events (ufcstats_id, name, event_date)"
                " VALUES (:u, 'Other', '2024-03-03')"
            ),
            {"u": OTHER_HEX},
        )
        fight_id = conn.execute(
            text(
                "INSERT INTO fights (ufcstats_id, event_id, fighter_a_id, fighter_b_id,"
                " red_fighter_id, weight_class, gender, status, is_title_fight, scheduled_rounds,"
                " outcome, winner_id, method, decision_type, method_detail, finish_round,"
                " finish_time_sec, referee, has_round_stats, result_source)"
                " VALUES (:u, :e, 2, 3, 3, 'Lightweight', 'M', 'completed', false, 3,"
                " 'win', 2, 'decision', 'unanimous', 'Unanimous Decision', 3, 300, 'Ref', true,"
                " 'ufcstats') RETURNING id"
            ),
            {"u": HEX, "e": event_id},
        ).scalar_one()
        for fighter_id in (2, 3):
            conn.execute(
                text(
                    "INSERT INTO fight_totals (fight_id, fighter_id, knockdowns,"
                    " sig_strikes_landed, sig_strikes_att, total_strikes_landed,"
                    " total_strikes_att, takedowns_landed, takedowns_att, submission_att,"
                    " reversals, ctrl_sec)"
                    " VALUES (:f, :p, 0, 40, 80, 50, 100, 1, 3, 1, 0, 60)"
                ),
                {"f": fight_id, "p": fighter_id},
            )
    return {"fight": fight_id, "event": event_id}


def fingerprint(db) -> str:
    with db.connect() as conn:
        return features_input_fingerprint(conn)


def apply(db, sql: str, ids: dict[str, int]) -> None:
    with db.begin() as conn:
        conn.execute(text(sql), {"f": ids["fight"], "e": ids["event"]})


# -- every column that feeds a feature or a label moves the hash ------------------------------

_BUMP = "UPDATE fight_totals SET {c} = {c} + 1 WHERE fight_id = :f AND fighter_id = 2"

IN_MUTATIONS: dict[str, str] = {
    "fights.event_id": "UPDATE fights SET event_id = (SELECT id FROM events WHERE name = 'Other')"
    " WHERE id = :f",
    "fights.fighter_a_id": "UPDATE fights SET fighter_a_id = 1, winner_id = 3 WHERE id = :f",
    "fights.fighter_b_id": "UPDATE fights SET fighter_b_id = 4, winner_id = 2,"
    " red_fighter_id = NULL WHERE id = :f",
    "fights.outcome": "UPDATE fights SET outcome = 'no_contest', winner_id = NULL WHERE id = :f",
    "fights.winner_id": "UPDATE fights SET winner_id = 3 WHERE id = :f",
    "fights.method": "UPDATE fights SET method = 'ko_tko' WHERE id = :f",
    "fights.scheduled_rounds": "UPDATE fights SET scheduled_rounds = 5 WHERE id = :f",
    "fights.finish_round": "UPDATE fights SET finish_round = 2 WHERE id = :f",
    "fights.finish_time_sec": "UPDATE fights SET finish_time_sec = 301 WHERE id = :f",
    "fights.weight_class": "UPDATE fights SET weight_class = 'Welterweight' WHERE id = :f",
    "fights.is_title_fight": "UPDATE fights SET is_title_fight = true WHERE id = :f",
    "events.event_date": "UPDATE events SET event_date = '2024-02-02' WHERE id = :e",
    "fighters.dob": "UPDATE fighters SET dob = '1991-01-01' WHERE id = 2",
    "fighters.height_cm": "UPDATE fighters SET height_cm = 181 WHERE id = 2",
    "fighters.reach_cm": "UPDATE fighters SET reach_cm = 186 WHERE id = 2",
    "fighters.stance": "UPDATE fighters SET stance = 'Southpaw' WHERE id = 2",
    **{
        f"fight_totals.{c}": _BUMP.format(c=c)
        for c in (
            "sig_strikes_landed",
            "sig_strikes_att",
            "takedowns_landed",
            "takedowns_att",
            "submission_att",
            "knockdowns",
        )
    },
}

# Primary-key columns: they change only when a row appears or disappears (tested below).
STRUCTURAL_IN = {"fights.id", "fighters.id", "fight_totals.fight_id", "fight_totals.fighter_id"}


@pytest.mark.parametrize("column", sorted(IN_MUTATIONS))
def test_a_column_that_feeds_a_feature_or_label_changes_the_fingerprint(db, column):
    ids = make_world(db)
    before = fingerprint(db)

    apply(db, IN_MUTATIONS[column], ids)

    assert fingerprint(db) != before


# -- columns that cannot reach a feature or a label leave it alone -------------------------


OUT_MUTATIONS: dict[str, str] = {
    "fights.ufcstats_id": "UPDATE fights SET ufcstats_id = '1111111111111111' WHERE id = :f",
    "fights.red_fighter_id": "UPDATE fights SET red_fighter_id = 2 WHERE id = :f",
    "fights.gender": "UPDATE fights SET gender = 'F' WHERE id = :f",
    "fights.result_source": "UPDATE fights SET result_source = 'wikipedia' WHERE id = :f",
    "fights.decision_type": "UPDATE fights SET decision_type = 'split' WHERE id = :f",
    "fights.method_detail": "UPDATE fights SET method_detail = 'Other text' WHERE id = :f",
    "fights.referee": "UPDATE fights SET referee = 'Someone Else' WHERE id = :f",
    "fights.has_round_stats": "UPDATE fights SET has_round_stats = false WHERE id = :f",
    "events.ufcstats_id": "UPDATE events SET ufcstats_id = '2222222222222222' WHERE id = :e",
    "events.wikipedia_article_id": "UPDATE events SET wikipedia_article_id = 77 WHERE id = :e",
    "events.name": "UPDATE events SET name = 'Renamed' WHERE id = :e",
    "events.city": "UPDATE events SET city = 'Elsewhere' WHERE id = :e",
    "events.state": "UPDATE events SET state = 'Elsewhere' WHERE id = :e",
    "events.country": "UPDATE events SET country = 'Elsewhere' WHERE id = :e",
    "fighters.ufcstats_id": "UPDATE fighters SET ufcstats_id = '3333333333333333' WHERE id = 2",
    "fighters.wikipedia_title": "UPDATE fighters SET wikipedia_title = 'Some_Link' WHERE id = 2",
    "fighters.name": "UPDATE fighters SET name = 'New Spelling' WHERE id = 2",
    **{
        f"fight_totals.{c}": _BUMP.format(c=c)
        for c in ("total_strikes_landed", "total_strikes_att", "reversals", "ctrl_sec")
    },
}

# Not a mutation of a column: `status` is set membership, `events.id` is carried by
# fights.event_id. Both are tested explicitly below.
OUT_BY_OTHER_MEANS = {"fights.status", "events.id"}


@pytest.mark.parametrize("column", sorted(OUT_MUTATIONS))
def test_a_bookkeeping_identity_or_display_column_leaves_the_fingerprint_alone(db, column):
    ids = make_world(db)
    before = fingerprint(db)

    apply(db, OUT_MUTATIONS[column], ids)

    assert fingerprint(db) == before


# -- rows that cannot reach a training row leave it alone --------------------------------------

UNRELATED_ROWS: dict[str, str] = {
    "a scheduled bout between two new fighters on a new card": """
        INSERT INTO fighters (id, ufcstats_id, name) VALUES (5, 'aaaaaaaaaaaaaaa5', 'Five');
        INSERT INTO fighters (id, ufcstats_id, name) VALUES (6, 'aaaaaaaaaaaaaaa6', 'Six');
        INSERT INTO events (ufcstats_id, name, event_date)
            VALUES ('bbbbbbbbbbbbbbb1', 'Upcoming', '2030-01-01');
        INSERT INTO fights (ufcstats_id, event_id, fighter_a_id, fighter_b_id, gender, status,
            has_round_stats) VALUES ('ccccccccccccccc1',
            (SELECT id FROM events WHERE name = 'Upcoming'), 5, 6, 'M', 'scheduled', false)""",
    "a bio edit for a fighter who only has a scheduled bout": """
        INSERT INTO fighters (id, ufcstats_id, name, height_cm) VALUES (5, 'aaaaaaaaaaaaaaa5',
            'Five', 170);
        INSERT INTO events (ufcstats_id, name, event_date)
            VALUES ('bbbbbbbbbbbbbbb1', 'Upcoming', '2030-01-01');
        INSERT INTO fights (ufcstats_id, event_id, fighter_a_id, fighter_b_id, gender, status,
            has_round_stats) VALUES ('ccccccccccccccc1',
            (SELECT id FROM events WHERE name = 'Upcoming'), 1, 5, 'M', 'scheduled', false);
        UPDATE fighters SET height_cm = 171 WHERE id = 5""",
    "a cancelled bout": """
        INSERT INTO fights (ufcstats_id, event_id, fighter_a_id, fighter_b_id, gender, status,
            has_round_stats) VALUES ('ccccccccccccccc2',
            (SELECT id FROM events WHERE name = 'Other'), 1, 4, 'M', 'cancelled', false)""",
    "a fighter with no fights": """
        INSERT INTO fighters (id, ufcstats_id, name, dob, height_cm, reach_cm, stance)
            VALUES (7, 'aaaaaaaaaaaaaaa7', 'Seven', '1999-09-09', 190, 195, 'Southpaw')""",
    "an event with no fights": """
        INSERT INTO events (ufcstats_id, name, event_date)
            VALUES ('bbbbbbbbbbbbbbb2', 'Empty', '2025-05-05')""",
    "odds for the completed fight": """
        INSERT INTO odds (fight_id, source, decimal_odds_a, decimal_odds_b)
            VALUES ((SELECT id FROM fights WHERE ufcstats_id = 'abcdef0123456789'),
            'mdabbert', 1.5, 2.6)""",
    "a per-round stats row": """
        INSERT INTO fight_round_stats (fight_id, fighter_id, round, knockdowns, head_landed)
            VALUES ((SELECT id FROM fights WHERE ufcstats_id = 'abcdef0123456789'), 2, 1, 0, 9)""",
    "a cached raw page": """
        INSERT INTO raw_pages (url, source, fetched_at, status, html)
            VALUES ('http://example.test/x', 'ufcstats', now(), 200, '<html></html>')""",
    "a load run": """
        INSERT INTO load_runs (source, file_path, sha256)
            VALUES ('silver', 'x.csv', repeat('0', 64))""",
}


@pytest.mark.parametrize("what", sorted(UNRELATED_ROWS))
def test_a_row_that_cannot_reach_a_training_row_leaves_the_fingerprint_alone(db, what):
    make_world(db)
    before = fingerprint(db)

    with db.begin() as conn:
        for statement in UNRELATED_ROWS[what].split(";"):
            if statement.strip():
                conn.execute(text(statement))

    assert fingerprint(db) == before


# -- row membership and identity -----------------------------------------------------------


def test_a_completed_fight_leaving_the_completed_set_changes_the_fingerprint(db):
    ids = make_world(db)
    before = fingerprint(db)

    apply(
        db,
        "UPDATE fights SET status = 'cancelled', outcome = NULL, method = NULL, winner_id = NULL,"
        " has_round_stats = false, result_source = NULL, is_title_fight = NULL WHERE id = :f",
        ids,
    )

    assert fingerprint(db) != before


def test_a_new_completed_fight_changes_the_fingerprint(db):
    make_world(db)
    before = fingerprint(db)

    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO fights (ufcstats_id, event_id, fighter_a_id, fighter_b_id, gender,"
                " status, is_title_fight, outcome, method, has_round_stats, result_source)"
                " VALUES ('dddddddddddddddd', (SELECT id FROM events WHERE name = 'Other'), 1, 4,"
                " 'M', 'completed', false, 'draw', 'decision', false, 'ufcstats')"
            )
        )

    assert fingerprint(db) != before


@pytest.mark.parametrize("fighter_id", [2, 3])
def test_a_stats_row_appearing_or_disappearing_changes_the_fingerprint(db, fighter_id):
    ids = make_world(db)
    before = fingerprint(db)

    with db.begin() as conn:
        conn.execute(
            text("DELETE FROM fight_totals WHERE fight_id = :f AND fighter_id = :p"),
            {"f": ids["fight"], "p": fighter_id},
        )

    assert fingerprint(db) != before


def test_the_fingerprint_is_stable_and_does_not_depend_on_row_order_or_the_transaction(db):
    make_world(db)

    first = fingerprint(db)
    with db.begin() as conn:
        # Rewriting a row moves its physical position; the hash is ordered by key.
        conn.execute(text("UPDATE fighters SET name = name WHERE id = 2"))
        conn.execute(text("UPDATE fight_totals SET knockdowns = knockdowns"))
    second = fingerprint(db)

    assert first == second
    assert len(first) == 64 and first == first.lower()


def test_an_empty_database_has_a_fingerprint_that_a_populated_one_does_not_share(db):
    empty = fingerprint(db)
    assert empty == fingerprint(db)

    make_world(db)

    assert fingerprint(db) != empty


# -- every column is classified ------------------------------------------------------------


@pytest.mark.parametrize("table", sorted({*INCLUDED, *EXCLUDED}))
def test_every_column_of_a_hashed_table_is_included_or_excluded_with_a_reason(table):
    columns = {c.name for c in Base.metadata.tables[table].columns}
    included = set(INCLUDED.get(table, ()))
    excluded = set(EXCLUDED.get(table, {}))

    assert included & excluded == set(), "a column cannot be both"
    assert columns == included | excluded, (
        f"{table}: classify {sorted(columns - included - excluded)} in fingerprint.py "
        f"(and drop {sorted((included | excluded) - columns)})"
    )
    assert all(EXCLUDED.get(table, {}).values()), "every exclusion needs a reason"


def test_a_table_left_out_whole_is_a_real_table_with_a_reason():
    for table, reason in EXCLUDED_TABLES.items():
        assert table in Base.metadata.tables
        assert reason
        assert table not in INCLUDED


def test_the_mutation_tables_cover_exactly_the_classified_columns():
    """Behaviour and classification can't drift: every classified column has a test above."""
    included = {f"{t}.{c}" for t, cols in INCLUDED.items() for c in cols}
    excluded = {f"{t}.{c}" for t, cols in EXCLUDED.items() for c in cols}

    assert set(IN_MUTATIONS) | STRUCTURAL_IN == included
    assert set(OUT_MUTATIONS) | OUT_BY_OTHER_MEANS == excluded
