"""Re-running an ingestion write with identical data must leave the features input fingerprint
unchanged (D-033).

The staleness guard refuses to train when the fingerprint moved. If an upsert path ever bumped a
hashed column without changing data (a timestamp, a counter, a delete-and-reinsert), every scheduled
scrape would make the features look stale and training would be blocked for nothing. These run the
real writers twice and compare."""

from sqlalchemy import text

from cageops_common.db.fingerprint import features_input_fingerprint
from cageops_worker.ingest import store
from cageops_worker.ingest.mapping import map_event, map_fight, map_fighter
from cageops_worker.ingest.mapping_wikipedia import map_bout
from cageops_worker.ingest.store_wikipedia import write_wikipedia_event

KO = "32054bf2b36b0e47"
BURNS = "23024fdfc966410a"


def fingerprint(db) -> str:
    with db.connect() as conn:
        return features_input_fingerprint(conn)


def hashed_row_versions(db) -> str:
    """The physical row versions of every hashed row. Unchanged means nothing was rewritten."""
    with db.connect() as conn:
        return conn.execute(
            text(
                "SELECT string_agg(src || ':' || xmin::text, ',' ORDER BY src, k) FROM ("
                " SELECT 'f' AS src, id::text AS k, xmin FROM fights"
                " UNION ALL SELECT 'e', id::text, xmin FROM events"
                " UNION ALL SELECT 'p', id::text, xmin FROM fighters"
                " UNION ALL SELECT 't', fight_id || '-' || fighter_id, xmin FROM fight_totals"
                ") x"
            )
        ).scalar_one()


def test_rerunning_the_ufcstats_writers_with_identical_data_leaves_the_fingerprint_alone(
    db, burns_card, fight, fighter, bout_for
):
    def run_all():
        with db.begin() as conn:
            event_counts, _ = store.write_event(conn, map_event(burns_card))
            bio = store.write_fighter_bio(conn, map_fighter(fighter(BURNS)))
            fight_result = store.write_completed_fight(conn, map_fight(fight(KO), bout_for(KO)))
        return event_counts, bio, fight_result

    run_all()
    first = fingerprint(db)
    versions = hashed_row_versions(db)

    event_counts, bio, fight_result = run_all()

    assert event_counts.unchanged == 1
    assert bio.unchanged == 1
    assert fight_result.fight.unchanged == 1
    assert (fight_result.totals.unchanged, fight_result.rounds.unchanged) == (2, 6)
    assert fight_result.stale_stat_rows_deleted == 0
    assert fingerprint(db) == first
    assert hashed_row_versions(db) == versions  # and not one hashed row was rewritten


def test_the_fingerprint_is_not_empty_after_a_real_fight_is_written(
    db, burns_card, fight, fighter, bout_for
):
    """So the test above compares something: the hash really does move when a fight lands."""
    empty = fingerprint(db)
    with db.begin() as conn:
        store.write_event(conn, map_event(burns_card))
        store.write_fighter_bio(conn, map_fighter(fighter(BURNS)))
        store.write_completed_fight(conn, map_fight(fight(KO), bout_for(KO)))

    assert fingerprint(db) != empty


def test_a_blank_page_that_changes_no_data_leaves_the_fingerprint_alone(
    db, burns_card, fight, fighter, bout_for
):
    """The `fill` policy: a blank on a page never erases a held value, so a worse page is a
    no-op."""
    with db.begin() as conn:
        store.write_event(conn, map_event(burns_card))
        store.write_fighter_bio(conn, map_fighter(fighter(BURNS)))
        store.write_completed_fight(conn, map_fight(fight(KO), bout_for(KO)))
    before = fingerprint(db)

    blank = map_fighter(fighter(BURNS)) | {"dob": None, "height_cm": None, "stance": None}
    with db.begin() as conn:
        store.write_fighter_bio(conn, blank)

    assert fingerprint(db) == before


def test_rerunning_the_wikipedia_writer_leaves_the_fingerprint_alone(db, wiki_page):
    page = wiki_page("UFC_323")

    def run():
        with db.begin() as conn:
            return write_wikipedia_event(conn, page, [map_bout(b) for b in page.bouts])

    run()
    first = fingerprint(db)
    versions = hashed_row_versions(db)

    again = run()

    assert again.fights.unchanged == len(page.bouts) and again.fights.updated == 0
    assert fingerprint(db) == first
    assert hashed_row_versions(db) == versions
