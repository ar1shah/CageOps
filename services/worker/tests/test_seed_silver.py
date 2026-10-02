import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import text

from cageops_worker.seed.normalize import UnknownValueError
from cageops_worker.seed.silver import load_silver, parse_silver_row


def test_parse_maps_slots_cleans_text_and_keeps_only_rounds_with_data(silver_row):
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


def test_parse_fight_without_round_data_is_flagged_not_zero_filled(silver_row):
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


def test_parse_title_name_in_weight_class_sets_title_flag(silver_row):
    parsed = parse_silver_row(silver_row(weight_class="UFC Middleweight Title", title_fight=False))

    assert parsed.fight["weight_class"] == "Middleweight"
    assert parsed.fight["is_title_fight"] is True
    assert parsed.title_from_weight_class is True


def test_parse_winner_matching_neither_fighter_is_unresolved(silver_row):
    parsed = parse_silver_row(silver_row(winner=None))

    assert parsed.unresolved_winner is True


def test_parse_unknown_result_fails_the_load(silver_row):
    with pytest.raises(UnknownValueError):
        parse_silver_row(silver_row(result="Overturned"))


def write_parquet(tmp_path, rows):
    path = tmp_path / "silver.parquet"
    pq.write_table(pa.Table.from_pylist(rows), path)
    return path


def snapshot(engine):
    with engine.connect() as conn:
        return {
            table: conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
            for table in ("fighters", "events", "fights", "fight_round_stats", "fight_totals")
        }


def test_load_is_idempotent_and_uses_neutral_fighter_order(db, tmp_path, silver_row, second_fight):
    path = write_parquet(tmp_path, [silver_row(), silver_row(**second_fight)])

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


def test_fighter_ids_follow_ufcstats_id_order_not_fight_order(db, tmp_path, silver_row):
    path = write_parquet(tmp_path, [silver_row()])

    load_silver(db, path, "x")

    with db.connect() as conn:
        names = conn.execute(text("SELECT name FROM fighters ORDER BY id")).scalars().all()
    # ufcstats ids: Adesanya "aaaa..." < Whittaker "zzzz...", although Whittaker is slot 1
    assert names == ["Israel Adesanya", "Robert Whittaker"]


def test_reload_updates_changed_values_instead_of_duplicating(db, tmp_path, silver_row):
    load_silver(db, write_parquet(tmp_path, [silver_row()]), "v1")

    load_silver(db, write_parquet(tmp_path, [silver_row(referee="Marc Goddard")]), "v2")

    with db.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM fights")).scalar_one() == 1
        assert conn.execute(text("SELECT referee FROM fights")).scalar_one() == "Marc Goddard"
