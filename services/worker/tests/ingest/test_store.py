import dataclasses
from datetime import date

import pytest
from sqlalchemy import text

from cageops_worker.ingest import store
from cageops_worker.ingest.errors import MissingPrerequisite
from cageops_worker.ingest.mapping import (
    map_event,
    map_fight,
    map_fighter,
    map_scheduled_bout,
)
from cageops_worker.ingest.upsert import UpsertCounts

KO = "32054bf2b36b0e47"  # Burns (red, lost) vs Malott (won), on the Burns card
BURNS, MALOTT = "23024fdfc966410a", "dd6103dd7127db1d"
BURNS_EVENT = "c3ac8d0da7b05772"
UPCOMING_EVENT = "7f98d9d5a10fa25c"


def fight_row(db, ufcstats_id):
    with db.connect() as conn:
        row = conn.execute(
            text(
                "SELECT f.*, a.ufcstats_id AS a_u, b.ufcstats_id AS b_u, r.ufcstats_id AS red_u,"
                " w.ufcstats_id AS win_u FROM fights f JOIN fighters a ON a.id = f.fighter_a_id"
                " JOIN fighters b ON b.id = f.fighter_b_id"
                " LEFT JOIN fighters r ON r.id = f.red_fighter_id"
                " LEFT JOIN fighters w ON w.id = f.winner_id WHERE f.ufcstats_id = :u"
            ),
            {"u": ufcstats_id},
        ).one_or_none()
    return None if row is None else row._mapping


def scalar(db, sql, **params):
    with db.connect() as conn:
        return conn.execute(text(sql), params).scalar()


@pytest.fixture
def burns_event(db, burns_card):
    with db.begin() as conn:
        store.write_event(conn, map_event(burns_card))


@pytest.fixture
def upcoming_event(db, upcoming_card):
    with db.begin() as conn:
        store.write_event(conn, map_event(upcoming_card))


def scheduled(upcoming_card):
    return [map_scheduled_bout(b, upcoming_card.ufcstats_id) for b in upcoming_card.bouts]


# -- events and fighters ---------------------------------------------------------------


def test_an_event_is_written_once_and_a_rerun_changes_nothing(db, burns_card):
    row = map_event(burns_card)
    with db.begin() as conn:
        first, event_db_id = store.write_event(conn, row)
    with db.begin() as conn:
        again, same_id = store.write_event(conn, row)

    assert first == UpsertCounts(inserted=1)
    assert again == UpsertCounts(unchanged=1) and same_id == event_db_id
    assert scalar(db, "SELECT event_date FROM events") == date(2026, 4, 18)


def test_an_event_rename_is_an_update_but_a_blank_location_never_erases(db, burns_card):
    row = map_event(burns_card)
    with db.begin() as conn:
        store.write_event(conn, row)
    with db.begin() as conn:
        counts, _ = store.write_event(
            conn, row | {"name": "UFC Fight Night: Renamed", "state": None}
        )

    assert counts.updated == 1
    assert scalar(db, "SELECT name FROM events") == "UFC Fight Night: Renamed"
    assert scalar(db, "SELECT state FROM events") == "Manitoba"


def test_fighter_stubs_are_created_once_and_never_overwrite_an_existing_row(db, fighter):
    with db.begin() as conn:
        store.write_fighter_bio(conn, map_fighter(fighter(BURNS)))
    with db.begin() as conn:
        counts, ids = store.ensure_fighters(
            conn,
            [
                {"ufcstats_id": BURNS, "name": "Stub Name"},
                {"ufcstats_id": MALOTT, "name": "Mike Malott"},
            ],
        )

    assert counts == UpsertCounts(inserted=1, unchanged=1)
    assert set(ids) == {BURNS, MALOTT}
    assert (
        scalar(db, "SELECT name FROM fighters WHERE ufcstats_id = :u", u=BURNS) == "Gilbert Burns"
    )
    assert scalar(db, "SELECT height_cm FROM fighters WHERE ufcstats_id = :u", u=BURNS) == 177.8


def test_a_fighter_bio_rerun_changes_nothing(db, fighter):
    row = map_fighter(fighter(BURNS))
    with db.begin() as conn:
        assert store.write_fighter_bio(conn, row).inserted == 1
    with db.begin() as conn:
        assert store.write_fighter_bio(conn, row) == UpsertCounts(unchanged=1)


def test_a_blank_bio_on_the_page_never_erases_what_we_hold(db, fighter):
    with db.begin() as conn:
        store.write_fighter_bio(conn, map_fighter(fighter(BURNS)))
    blank = map_fighter(fighter(BURNS)) | {"dob": None, "height_cm": None, "stance": None}
    with db.begin() as conn:
        counts = store.write_fighter_bio(conn, blank)

    assert counts == UpsertCounts(unchanged=1)
    assert scalar(db, "SELECT dob FROM fighters") == date(1986, 7, 20)
    assert scalar(db, "SELECT stance FROM fighters") == "Orthodox"


def test_a_bio_the_site_filled_in_later_is_filled_in_here(db, fighter):
    """Rahiki: the seed has NULL reach and DOB; the page now has both."""
    rahiki = "6eedb757f13b9978"
    with db.begin() as conn:
        store.ensure_fighters(conn, [{"ufcstats_id": rahiki, "name": "Marwan Rahiki"}])
        counts = store.write_fighter_bio(conn, map_fighter(fighter(rahiki)))

    assert counts.updated == 1
    assert scalar(db, "SELECT reach_cm FROM fighters") == 182.88
    assert scalar(db, "SELECT dob FROM fighters") == date(2002, 5, 10)


# -- a fight that happened -------------------------------------------------------------


def test_a_fight_needs_its_event_row_first(db, fight, bout_for):
    with (
        pytest.raises(MissingPrerequisite, match="takes its date from the event page"),
        db.begin() as conn,
    ):
        store.write_completed_fight(conn, map_fight(fight(KO), bout_for(KO)))

    assert scalar(db, "SELECT count(*) FROM fights") == 0


def test_a_completed_fight_is_written_with_its_stats(db, burns_event, fight, bout_for):
    with db.begin() as conn:
        result = store.write_completed_fight(conn, map_fight(fight(KO), bout_for(KO)))

    row = fight_row(db, KO)
    assert (row["status"], row["outcome"], row["method"]) == ("completed", "win", "ko_tko")
    assert (row["red_u"], row["win_u"]) == (BURNS, MALOTT)
    assert (row["finish_round"], row["finish_time_sec"], row["referee"]) == (3, 128, "Herb Dean")
    assert row["has_round_stats"] is True
    assert result.fight.inserted == 1 and result.previous_status is None
    assert (result.totals.inserted, result.rounds.inserted) == (2, 6)
    assert scalar(db, "SELECT count(*) FROM completed_fights") == 1


def test_a_rerun_writes_nothing_at_all(db, burns_event, fight, bout_for):
    mapped = map_fight(fight(KO), bout_for(KO))
    with db.begin() as conn:
        store.write_completed_fight(conn, mapped)
    before = scalar(db, "SELECT string_agg(xmin::text, ',') FROM (SELECT xmin FROM fights"
                        " UNION ALL SELECT xmin FROM fight_totals UNION ALL"
                        " SELECT xmin FROM fight_round_stats) x")  # fmt: skip

    with db.begin() as conn:
        again = store.write_completed_fight(conn, mapped)

    assert again.fight == UpsertCounts(unchanged=1)
    assert again.totals == UpsertCounts(unchanged=2) and again.rounds == UpsertCounts(unchanged=6)
    assert again.previous_status == "completed" and again.anomalies == []
    after = scalar(db, "SELECT string_agg(xmin::text, ',') FROM (SELECT xmin FROM fights"
                       " UNION ALL SELECT xmin FROM fight_totals UNION ALL"
                       " SELECT xmin FROM fight_round_stats) x")  # fmt: skip
    assert after == before  # not a single row version was rewritten


def test_the_two_fighters_are_in_neutral_order_whatever_order_they_arrived_in(
    db, burns_event, fight, bout_for
):
    with db.begin() as conn:
        store.ensure_fighters(conn, [{"ufcstats_id": MALOTT, "name": "Mike Malott"}])  # lower id
        store.write_completed_fight(conn, map_fight(fight(KO), bout_for(KO)))

    row = fight_row(db, KO)
    assert row["a_u"] == MALOTT and row["b_u"] == BURNS  # a < b by database id
    assert row["fighter_a_id"] < row["fighter_b_id"]
    assert row["red_u"] == BURNS  # red is a fact about the corner, not about the order
    assert row["win_u"] == MALOTT


def test_an_overturned_result_clears_the_old_winner_and_is_reported(
    db, burns_event, fight, bout_for
):
    page = fight(KO)
    with db.begin() as conn:
        store.write_completed_fight(conn, map_fight(page, bout_for(KO)))
    overturned = page.model_copy(
        update={
            "fighters": tuple(f.model_copy(update={"result": "NC"}) for f in page.fighters),
            "method_raw": "Could Not Continue",
            "details_raw": None,
        }
    )

    with db.begin() as conn:
        result = store.write_completed_fight(conn, map_fight(overturned))

    row = fight_row(db, KO)
    assert (row["outcome"], row["win_u"], row["method"]) == ("no_contest", None, "other")
    assert row["status"] == "completed"
    assert result.fight.updated == 1
    [anomaly] = result.anomalies
    assert anomaly.startswith(f"result_changed:fight={KO}:")
    assert "outcome=win->no_contest" in anomaly and "method=ko_tko->other" in anomaly


def test_a_blank_stat_cell_never_erases_a_number_we_hold(db, burns_event, fight, bout_for):
    mapped = map_fight(fight(KO), bout_for(KO))
    with db.begin() as conn:
        store.write_completed_fight(conn, mapped)
    blanked = dataclasses.replace(
        mapped, totals=[{**t, "ctrl_sec": None, "sig_strikes_landed": 41} for t in mapped.totals]
    )

    with db.begin() as conn:
        result = store.write_completed_fight(conn, blanked)

    assert result.totals.updated == 2  # sig strikes were corrected from 40/56 to 41
    assert scalar(db, "SELECT ctrl_sec FROM fight_totals ORDER BY ctrl_sec DESC LIMIT 1") == 14
    assert scalar(db, "SELECT count(*) FROM fight_totals WHERE ctrl_sec IS NULL") == 0
    assert scalar(db, "SELECT count(*) FROM fight_totals WHERE sig_strikes_landed = 41") == 2


def test_a_round_that_is_no_longer_on_the_page_is_removed(db, burns_event, fight, bout_for):
    mapped = map_fight(fight(KO), bout_for(KO))
    with db.begin() as conn:
        store.write_completed_fight(conn, mapped)
    shorter = dataclasses.replace(mapped, rounds=[r for r in mapped.rounds if r["round"] <= 2])

    with db.begin() as conn:
        result = store.write_completed_fight(conn, shorter)

    assert result.stale_stat_rows_deleted == 2  # round 3, both fighters
    assert scalar(db, "SELECT count(*) FROM fight_round_stats") == 4


def test_a_page_with_no_stats_never_deletes_the_stats_we_hold(db, burns_event, fight, bout_for):
    mapped = map_fight(fight(KO), bout_for(KO))
    with db.begin() as conn:
        store.write_completed_fight(conn, mapped)
    empty = dataclasses.replace(mapped, totals=[], rounds=[])
    empty.fight["has_round_stats"] = False  # the page says there are none

    with db.begin() as conn:
        result = store.write_completed_fight(conn, empty)

    assert result.stale_stat_rows_deleted == 0
    assert scalar(db, "SELECT count(*) FROM fight_round_stats") == 6
    assert fight_row(db, KO)["has_round_stats"] is True  # never goes back to false


def test_a_guessed_gender_never_overwrites_a_known_one(db, burns_event, fight, bout_for):
    catch_weight = "552f7cdaf93e1055"  # the page can't say whether a catch-weight bout is M or F
    mapped = map_fight(fight(catch_weight), bout_for(catch_weight))
    assert mapped.fight["gender_guessed"] is True
    with db.begin() as conn:
        store.write_completed_fight(conn, mapped)
        conn.execute(text("UPDATE fights SET gender = 'F'"))  # what a real source would have said

    with db.begin() as conn:
        result = store.write_completed_fight(conn, mapped)

    assert fight_row(db, catch_weight)["gender"] == "F"
    assert result.fight == UpsertCounts(unchanged=1)


# -- scheduled bouts and the card ------------------------------------------------------


def test_scheduled_bouts_are_written_with_no_result(db, upcoming_event, upcoming_card):
    with db.begin() as conn:
        counts = store.write_scheduled_bouts(conn, UPCOMING_EVENT, scheduled(upcoming_card))

    assert counts == UpsertCounts(inserted=12)
    row = fight_row(db, "7db1a3dac7e343e7")
    assert row["status"] == "scheduled"
    assert (row["outcome"], row["method"], row["winner_id"], row["is_title_fight"]) == (None,) * 4
    assert (row["red_fighter_id"], row["has_round_stats"]) == (None, False)
    assert row["fighter_a_id"] < row["fighter_b_id"]
    assert scalar(db, "SELECT count(*) FROM completed_fights") == 0


def test_a_scheduled_rerun_changes_nothing(db, upcoming_event, upcoming_card):
    with db.begin() as conn:
        store.write_scheduled_bouts(conn, UPCOMING_EVENT, scheduled(upcoming_card))
    with db.begin() as conn:
        again = store.write_scheduled_bouts(conn, UPCOMING_EVENT, scheduled(upcoming_card))

    assert again == UpsertCounts(unchanged=12)


def test_a_scheduled_bout_never_overwrites_a_completed_fight(
    db, burns_event, burns_card, fight, bout_for
):
    with db.begin() as conn:
        store.write_completed_fight(conn, map_fight(fight(KO), bout_for(KO)))
    same_fight_as_scheduled = map_scheduled_bout(bout_for(KO), BURNS_EVENT)

    with db.begin() as conn:
        counts = store.write_scheduled_bouts(conn, BURNS_EVENT, [same_fight_as_scheduled])

    assert counts == UpsertCounts(unchanged=1)
    assert fight_row(db, KO)["status"] == "completed" and fight_row(db, KO)["outcome"] == "win"


def test_a_scheduled_bout_becomes_completed_when_its_result_arrives(
    db, burns_event, fight, bout_for
):
    with db.begin() as conn:
        store.write_scheduled_bouts(
            conn, BURNS_EVENT, [map_scheduled_bout(bout_for(KO), BURNS_EVENT)]
        )
    assert fight_row(db, KO)["status"] == "scheduled"

    with db.begin() as conn:
        result = store.write_completed_fight(conn, map_fight(fight(KO), bout_for(KO)))

    assert (
        result.previous_status == "scheduled" and result.anomalies == []
    )  # a transition, not a change
    assert (fight_row(db, KO)["status"], fight_row(db, KO)["win_u"]) == ("completed", MALOTT)


def test_a_bout_removed_from_the_card_is_cancelled_not_deleted(db, upcoming_event, upcoming_card):
    bouts = scheduled(upcoming_card)
    with db.begin() as conn:
        store.write_scheduled_bouts(conn, UPCOMING_EVENT, bouts)
        event_db_id = store.event_id(conn, UPCOMING_EVENT)
    on_page = [b.fight["ufcstats_id"] for b in bouts[:-1]]  # the last bout was taken off the card
    removed = bouts[-1].fight["ufcstats_id"]

    with db.begin() as conn:
        result = store.reconcile_event_bouts(conn, event_db_id, on_page)

    assert result.cancelled == [removed] and result.anomalies == []
    assert fight_row(db, removed)["status"] == "cancelled"
    assert scalar(db, "SELECT count(*) FROM fights") == 12  # still there, for the record
    assert scalar(db, "SELECT count(*) FROM completed_fights") == 0


def test_a_cancelled_bout_that_reappears_is_scheduled_again(db, upcoming_event, upcoming_card):
    bouts = scheduled(upcoming_card)
    removed = bouts[-1].fight["ufcstats_id"]
    with db.begin() as conn:
        store.write_scheduled_bouts(conn, UPCOMING_EVENT, bouts)
        store.reconcile_event_bouts(
            conn, store.event_id(conn, UPCOMING_EVENT), [b.fight["ufcstats_id"] for b in bouts[:-1]]
        )
    assert fight_row(db, removed)["status"] == "cancelled"

    with db.begin() as conn:
        counts = store.write_scheduled_bouts(conn, UPCOMING_EVENT, bouts)

    assert counts.updated == 1 and counts.unchanged == 11
    assert fight_row(db, removed)["status"] == "scheduled"


def test_a_page_with_no_bouts_cancels_nothing_and_says_so(db, upcoming_event, upcoming_card):
    with db.begin() as conn:
        store.write_scheduled_bouts(conn, UPCOMING_EVENT, scheduled(upcoming_card))
        event_db_id = store.event_id(conn, UPCOMING_EVENT)

    with db.begin() as conn:
        result = store.reconcile_event_bouts(conn, event_db_id, [])

    assert result.cancelled == []
    assert result.anomalies == [f"event_page_has_no_bouts:event_id={event_db_id}"]
    assert scalar(db, "SELECT count(*) FROM fights WHERE status = 'scheduled'") == 12


def test_a_completed_fight_missing_from_its_event_page_is_flagged_but_never_changed(
    db, burns_event, fight, bout_for
):
    with db.begin() as conn:
        store.write_completed_fight(conn, map_fight(fight(KO), bout_for(KO)))
        event_db_id = store.event_id(conn, BURNS_EVENT)

    with db.begin() as conn:
        result = store.reconcile_event_bouts(conn, event_db_id, ["ffffffffffffffff"])

    assert result.cancelled == []
    assert result.anomalies == [f"completed_fight_missing_from_event:fight={KO}"]
    assert fight_row(db, KO)["status"] == "completed"


def test_a_cancelled_event_cancels_its_scheduled_bouts_only(
    db, burns_event, upcoming_event, upcoming_card, fight, bout_for
):
    with db.begin() as conn:
        store.write_completed_fight(conn, map_fight(fight(KO), bout_for(KO)))
        store.write_scheduled_bouts(conn, UPCOMING_EVENT, scheduled(upcoming_card))
        event_db_id = store.event_id(conn, UPCOMING_EVENT)

    with db.begin() as conn:
        cancelled = store.cancel_scheduled_for_event(conn, event_db_id)

    assert len(cancelled) == 12
    assert scalar(db, "SELECT count(*) FROM fights WHERE status = 'cancelled'") == 12
    assert fight_row(db, KO)["status"] == "completed"


def test_scheduled_bouts_need_their_event_row_first(db, upcoming_card):
    with pytest.raises(MissingPrerequisite), db.begin() as conn:
        store.write_scheduled_bouts(conn, UPCOMING_EVENT, scheduled(upcoming_card))
