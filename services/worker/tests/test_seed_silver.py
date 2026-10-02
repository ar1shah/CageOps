from datetime import date

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import text

from cageops_worker.seed.normalize import UnknownValueError
from cageops_worker.seed.silver import load_silver, parse_silver_row


def silver_row(**overrides):
    """A sparse silver-style row: Whittaker (slot 1, red) vs Adesanya (slot 2), UFC 243."""
    row = {
        "fight_url": "http://ufcstats.com/fight-details/ffff000000000001",
        "event_url": "http://ufcstats.com/event-details/eeee000000000001",
        "event_name": "UFC 243: Whittaker vs. Adesanya",
        "event_date": date(2019, 10, 5),
        "event_city": "Melbourne",
        "event_state": "NULL",  # silver's literal-string null
        "event_country": "Australia",
        "f_1_url": "http://ufcstats.com/fighter-details/zzzz000000000001",
        "f_1_name": "Robert Whittaker",
        "f_2_url": "http://ufcstats.com/fighter-details/aaaa000000000002",
        "f_2_name": "Israel Adesanya",
        "f_1_fighter_height_cm": 182.88,
        "f_2_fighter_stance": "Switch",
        "winner": "Israel Adesanya",
        "result": "\n\n        \n KO/TKO \n",
        "result_details": " Punch to Head At Distance ",
        "weight_class": "Middleweight",
        "gender": "M",
        "title_fight": True,
        "num_rounds": 5,
        "finish_round": 2,
        "finish_time": "3:33",
        "referee": "",
        # round 1 and 2 only (the fight ended in round 2); rounds 3-5 are absent
        "f_1_r1_sig_strikes_succ": 17,
        "f_1_r1_sig_strikes_att": 66,
        "f_2_r1_sig_strikes_succ": 20,
        "f_2_r1_sig_strikes_att": 44,
        "f_2_r1_knockdowns": 1,
        "f_1_r2_sig_strikes_succ": 15,
        "f_1_r2_sig_strikes_att": 50,
        "f_2_r2_sig_strikes_succ": 20,
        "f_2_r2_sig_strikes_att": 51,
        "f_1_sig_strikes_succ": 32,
        "f_2_sig_strikes_succ": 40,
    }
    return row | overrides


def test_parse_maps_slots_cleans_text_and_keeps_only_rounds_with_data():
    parsed = parse_silver_row(silver_row())

    assert parsed.fight["method"] == "ko_tko"
    assert parsed.fight["method_detail"] == "Punch to Head At Distance"
    assert parsed.fight["finish_time_sec"] == 213
    assert parsed.fight["referee"] is None
    assert parsed.event["state"] is None
    assert parsed.fight["slot_winner"] == 2
    assert parsed.fighters[1]["stance"] == "Switch"
    assert sorted({(r["slot"], r["round"]) for r in parsed.round_stats}) == [
        (1, 1),
        (1, 2),
        (2, 1),
        (2, 2),
    ]  # no rows, and no zeros, for rounds 3-5
    assert parsed.fight["has_round_stats"] is True


def test_parse_fight_without_round_data_is_flagged_not_zero_filled():
    parsed = parse_silver_row(
        silver_row(
            f_1_r1_sig_strikes_succ=None,
            f_1_r1_sig_strikes_att=None,
            f_2_r1_sig_strikes_succ=None,
            f_2_r1_sig_strikes_att=None,
            f_2_r1_knockdowns=None,
            f_1_r2_sig_strikes_succ=None,
            f_1_r2_sig_strikes_att=None,
            f_2_r2_sig_strikes_succ=None,
            f_2_r2_sig_strikes_att=None,
        )
    )

    assert parsed.round_stats == []
    assert parsed.fight["has_round_stats"] is False
    assert len(parsed.totals) == 2  # totals still kept


def test_parse_title_name_in_weight_class_sets_title_flag():
    parsed = parse_silver_row(silver_row(weight_class="UFC Middleweight Title", title_fight=False))

    assert parsed.fight["weight_class"] == "Middleweight"
    assert parsed.fight["is_title_fight"] is True
    assert parsed.title_from_weight_class is True


def test_parse_winner_matching_neither_fighter_is_unresolved():
    parsed = parse_silver_row(silver_row(winner=None))

    assert parsed.unresolved_winner is True


def test_parse_unknown_result_fails_the_load():
    with pytest.raises(UnknownValueError):
        parse_silver_row(silver_row(result="Overturned"))


def write_parquet(tmp_path, rows):
    path = tmp_path / "silver.parquet"
    pq.write_table(pa.Table.from_pylist(rows), path)
    return path


# The second fight has no per-round or total stats at all (like the earliest UFC cards).
NO_STATS = {k: None for k in silver_row() if "sig_strikes" in k or "knockdowns" in k}
SECOND_FIGHT = NO_STATS | {
    "fight_url": "http://ufcstats.com/fight-details/ffff000000000002",
    "event_url": "http://ufcstats.com/event-details/eeee000000000002",
    "event_name": "UFC 254",
    "event_date": date(2020, 10, 24),
    "f_1_url": "http://ufcstats.com/fighter-details/mmmm000000000003",
    "f_1_name": "Khabib Nurmagomedov",
    "f_2_url": "http://ufcstats.com/fighter-details/bbbb000000000004",
    "f_2_name": "Justin Gaethje",
    "winner": "Khabib Nurmagomedov",
    "result": "Submission",
    "result_details": "Triangle Choke From Bottom Guard",
    "weight_class": "Lightweight",
    "gender": "M",
    "title_fight": True,
}


def snapshot(engine):
    with engine.connect() as conn:
        return {
            table: conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
            for table in ("fighters", "events", "fights", "fight_round_stats", "fight_totals")
        }


def test_load_is_idempotent_and_uses_neutral_fighter_order(db, tmp_path):
    path = write_parquet(tmp_path, [silver_row(), silver_row(**SECOND_FIGHT)])

    first = load_silver(db, path, "abc123")
    counts_after_first = snapshot(db)
    second = load_silver(db, path, "abc123")

    assert snapshot(db) == counts_after_first
    assert counts_after_first == {
        "fighters": 4,
        "events": 2,
        "fights": 2,
        "fight_round_stats": 4,
        "fight_totals": 2,
    }
    assert first["fights"] == second["fights"] == 2
    assert first["fights_without_round_stats"] == 1
    with db.connect() as conn:
        # slot 1 (the red corner) is stored in `red_fighter_id`, whichever id is smaller
        rows = conn.execute(
            text(
                "SELECT a.name, b.name, r.name, w.name FROM fights f"
                " JOIN fighters a ON a.id = f.fighter_a_id JOIN fighters b ON b.id = f.fighter_b_id"
                " JOIN fighters r ON r.id = f.red_fighter_id JOIN fighters w ON w.id = f.winner_id"
                " ORDER BY f.id"
            )
        ).all()
        runs = conn.execute(text("SELECT sha256, rows_read FROM load_runs")).all()
    assert rows[0][2] == "Robert Whittaker"
    assert rows[0][3] == "Israel Adesanya"
    assert {rows[0][0], rows[0][1]} == {"Robert Whittaker", "Israel Adesanya"}
    assert runs == [("abc123", 2), ("abc123", 2)]


def test_fighter_ids_follow_ufcstats_id_order_not_fight_order(db, tmp_path):
    path = write_parquet(tmp_path, [silver_row()])

    load_silver(db, path, "x")

    with db.connect() as conn:
        names = conn.execute(text("SELECT name FROM fighters ORDER BY id")).scalars().all()
    # ufcstats ids: Adesanya "aaaa..." < Whittaker "zzzz...", although Whittaker is slot 1
    assert names == ["Israel Adesanya", "Robert Whittaker"]


def test_reload_updates_changed_values_instead_of_duplicating(db, tmp_path):
    load_silver(db, write_parquet(tmp_path, [silver_row()]), "v1")

    load_silver(db, write_parquet(tmp_path, [silver_row(referee="Marc Goddard")]), "v2")

    with db.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM fights")).scalar_one() == 1
        assert conn.execute(text("SELECT referee FROM fights")).scalar_one() == "Marc Goddard"
