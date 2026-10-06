"""Code that assumed every row has a ufcstats id keeps working when another source's rows exist,
and a ufcstats result always replaces another source's (D-029)."""

from datetime import date

from sqlalchemy import text

from cageops_worker.ingest import jobs, store
from cageops_worker.ingest.mapping import map_event, map_fight
from cageops_worker.seed.resolver import load_aliases

KO = "32054bf2b36b0e47"
BURNS, MALOTT = "23024fdfc966410a", "dd6103dd7127db1d"


def scalar(db, sql, **params):
    with db.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def a_wikipedia_event_with_a_fight(db, article=1, day=date(2026, 10, 10), status="completed"):
    """An event and a fight another source created: no ufcstats id anywhere."""
    with db.begin() as conn:
        fighters = [
            conn.execute(
                text("INSERT INTO fighters (wikipedia_title, name) VALUES (:t, :t) RETURNING id"),
                {"t": f"Wiki_Fighter_{article}_{i}"},
            ).scalar_one()
            for i in (1, 2)
        ]
        event_id = conn.execute(
            text(
                "INSERT INTO events (wikipedia_article_id, name, event_date)"
                " VALUES (:a, 'Wiki card', :d) RETURNING id"
            ),
            {"a": article, "d": day},
        ).scalar_one()
        done = status == "completed"
        conn.execute(
            text(
                "INSERT INTO fights (event_id, fighter_a_id, fighter_b_id, gender, status,"
                " is_title_fight, outcome, method, has_round_stats, result_source)"
                " VALUES (:e, :a, :b, 'M', :s, :t, :o, :m, false, :src)"
            ),
            {
                "e": event_id,
                "a": min(fighters),
                "b": max(fighters),
                "s": status,
                "t": False if done else None,
                "o": "draw" if done else None,
                "m": "decision" if done else None,
                "src": "wikipedia" if done else None,
            },
        )
    return event_id


def test_a_completed_ufcstats_fight_is_a_ufcstats_result(db, burns_card, fight, bout_for):
    with db.begin() as conn:
        store.write_event(conn, map_event(burns_card))
        store.write_completed_fight(conn, map_fight(fight(KO), bout_for(KO)))

    assert scalar(db, "SELECT result_source FROM fights WHERE ufcstats_id = :u", u=KO) == "ufcstats"


def test_a_ufcstats_result_replaces_a_wikipedia_result_and_says_so(db, burns_card, fight, bout_for):
    with db.begin() as conn:
        store.write_event(conn, map_event(burns_card))
        store.write_completed_fight(conn, map_fight(fight(KO), bout_for(KO)))
    # Another source filled this fight's result differently (the winner flipped, a decision).
    with db.begin() as conn:
        conn.execute(
            text(
                "UPDATE fights SET result_source = 'wikipedia', method = 'decision',"
                " winner_id = fighter_a_id + fighter_b_id - winner_id WHERE ufcstats_id = :u"
            ),
            {"u": KO},
        )

    with db.begin() as conn:
        result = store.write_completed_fight(conn, map_fight(fight(KO), bout_for(KO)))

    assert scalar(db, "SELECT result_source FROM fights WHERE ufcstats_id = :u", u=KO) == "ufcstats"
    assert scalar(db, "SELECT method FROM fights WHERE ufcstats_id = :u", u=KO) == "ko_tko"
    assert [a.split(":")[0] for a in result.anomalies] == ["result_changed"]


def test_reconcile_ignores_fights_that_have_no_ufcstats_id(db):
    event_id = a_wikipedia_event_with_a_fight(db)

    with db.begin() as conn:
        result = store.reconcile_event_bouts(conn, event_id, ["a" * 16])

    assert result.anomalies == [] and result.cancelled == []  # no "completed_fight_missing: None"


def test_the_upcoming_recheck_never_builds_a_url_for_an_event_without_a_ufcstats_id(ctx, db):
    a_wikipedia_event_with_a_fight(db, status="scheduled")

    summary = jobs.discover_events("upcoming", "run", force=True)

    assert "rechecked" not in summary
    assert not [j for j in ctx.queue.job_ids if "None" in j]


def test_the_alias_loader_still_works_when_another_sources_fighters_exist(db, tmp_path):
    a_wikipedia_event_with_a_fight(db)  # two fighters with a NULL ufcstats_id
    csv = tmp_path / "aliases.csv"
    csv.write_text("source,alias,ufcstats_id,note\n")

    with db.begin() as conn:
        # Defensive filter: a None key in that dict is harmless, so this can't fail without it.
        assert load_aliases(conn, csv) == 0


def test_the_seed_id_map_skips_rows_without_a_ufcstats_id(db):
    from cageops_common.db.models import Event, Fighter
    from cageops_worker.seed.silver import _id_map

    a_wikipedia_event_with_a_fight(db)
    with db.begin() as conn:
        conn.execute(
            text("INSERT INTO fighters (ufcstats_id, name) VALUES (:u, 'Real')"), {"u": BURNS}
        )
        assert set(_id_map(conn, Fighter.__table__)) == {BURNS}
        assert _id_map(conn, Event.__table__) == {}
