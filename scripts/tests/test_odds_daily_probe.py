"""scripts/odds_daily_probe.py: matching, the consensus price, and that it is read-only and pinned
to a snapshot (D-034)."""

import csv
import hashlib
import io
from datetime import date

import odds_daily_probe as probe
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

HEADER = [
    "fight_url", "fighter_1_url", "fighter_2_url", "fighter_1", "fighter_2", "odds_1", "odds_2",
    "f1_ko_odds", "f2_ko_odds", "f1_sub_odds", "f2_sub_odds", "f1_dec_odds", "f2_dec_odds",
    "event_date", "adding_date", "source", "region",
]  # fmt: skip
ID_1, ID_2 = "a" * 16, "b" * 16


def feed_row(**kw):
    row = dict.fromkeys(HEADER, "")
    row.update(
        fight_url="", fighter_1="Ann Alpha", fighter_2="Bea Beta", odds_1="1.50", odds_2="2.80",
        event_date="2026-02-07", adding_date="2026-02-06 12:00:00+00:00",
        source="BookA", region="us",
    )  # fmt: skip
    row.update({k: str(v) for k, v in kw.items()})
    return row


def write_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=HEADER)
        writer.writeheader()
        writer.writerows(rows)
    return path


def add_fight(db, a, b, on, *, uid=None, winner="a", mdabbert=None):
    """Two fighters, an event and a completed fight (a < b by id). Returns the fight id."""
    with db.begin() as conn:
        ids = []
        for name in (a, b):
            ids.append(
                conn.execute(
                    text("INSERT INTO fighters (ufcstats_id, name) VALUES (:u, :n) RETURNING id"),
                    {"u": f"{abs(hash(name)) % 10**15:016x}", "n": name},
                ).scalar_one()
            )
        low, high = sorted(ids)
        event = conn.execute(
            text(
                "INSERT INTO events (ufcstats_id, name, event_date)"
                " VALUES (:u, :n, :d) RETURNING id"
            ),
            {"u": f"{abs(hash((a, b, on))) % 10**15:016x}", "n": f"E {on}", "d": on},
        ).scalar_one()
        fight = conn.execute(
            text(
                "INSERT INTO fights (ufcstats_id, event_id, fighter_a_id, fighter_b_id, gender,"
                " status, is_title_fight, outcome, winner_id, method, has_round_stats,"
                " result_source) VALUES (:u, :e, :a, :b, 'M', 'completed', false, 'win', :w,"
                " 'decision', false, 'ufcstats') RETURNING id"
            ),
            {"u": uid, "e": event, "a": low, "b": high, "w": low if winner == "a" else high},
        ).scalar_one()
        if mdabbert:
            conn.execute(
                text(
                    "INSERT INTO odds (fight_id, source, decimal_odds_a, decimal_odds_b)"
                    " VALUES (:f, 'mdabbert', :a, :b)"
                ),
                {"f": fight, "a": mdabbert[0], "b": mdabbert[1]},
            )
    return fight


def ours(db):
    with db.connect() as conn:
        return probe.load_our_fights(conn)


def feed_of(tmp_path, *rows):
    return probe.Feed.build(probe.parse_feed(write_csv(tmp_path / "f.csv", list(rows))))


# -- matching --------------------------------------------------------------------------------


def test_a_row_with_the_ufcstats_id_matches_by_id_even_if_the_names_are_spelled_differently(
    db, tmp_path
):
    add_fight(db, "Khalil Rountree Jr.", "Jiri Prochazka", date(2026, 2, 7), uid=ID_1)
    row = feed_row(
        fight_url=f"http://ufcstats.com/fight-details/{ID_1}",
        fighter_1="Khalil Rountree", fighter_2="Jiri Prochazka",
    )  # fmt: skip

    how, rows = probe.match_fight(ours(db)[0], feed_of(tmp_path, row))

    assert how == "id" and len(rows) == 1


def test_a_blank_fight_url_is_never_treated_as_an_id(db, tmp_path):
    add_fight(db, "Ann Alpha", "Bea Beta", date(2026, 2, 7), uid=None)

    how, rows = probe.match_fight(ours(db)[0], feed_of(tmp_path, feed_row(fight_url="")))

    assert how == "name+date" and len(rows) == 1  # not "id", and not matched on None == None


def test_names_match_in_either_order_and_one_day_either_side(db, tmp_path):
    add_fight(db, "Ann Alpha", "Bea Beta", date(2026, 2, 7))
    swapped = feed_row(fighter_1="Bea Beta", fighter_2="Ann Alpha", event_date="2026-02-08")

    how, _ = probe.match_fight(ours(db)[0], feed_of(tmp_path, swapped))

    assert how == "name+date"


def test_two_days_off_is_not_a_match(db, tmp_path):
    add_fight(db, "Ann Alpha", "Bea Beta", date(2026, 2, 7))

    how, rows = probe.match_fight(ours(db)[0], feed_of(tmp_path, feed_row(event_date="2026-02-09")))

    assert how is None and rows == []


def test_a_row_without_both_prices_does_not_count_as_a_match(db, tmp_path):
    add_fight(db, "Ann Alpha", "Bea Beta", date(2026, 2, 7))

    how, _ = probe.match_fight(ours(db)[0], feed_of(tmp_path, feed_row(odds_2="")))

    assert how is None


# -- why a fight does not match --------------------------------------------------------------


def classify(db, tmp_path, *rows):
    return probe.classify_unmatched(ours(db)[0], feed_of(tmp_path, *rows))


def test_a_bout_nobody_listed(db, tmp_path):
    add_fight(db, "Ann Alpha", "Bea Beta", date(2026, 2, 7))

    unrelated = feed_row(fighter_1="Cy Gamma", fighter_2="Di Delta")
    assert classify(db, tmp_path, unrelated) == "bout not in feed"


def test_one_exact_name_and_a_similar_other_is_a_spelling_variant(db, tmp_path):
    add_fight(db, "Ann Alpha", "Michael Aswell Jr.", date(2026, 2, 7))

    variant = feed_row(fighter_1="Ann Alpha", fighter_2="Michael Aswell")

    assert classify(db, tmp_path, variant) == "spelling variant"


def test_one_exact_name_and_an_unrelated_other_is_a_different_opponent(db, tmp_path):
    add_fight(db, "Ann Alpha", "Bea Beta", date(2026, 2, 7))

    replaced = feed_row(fighter_1="Ann Alpha", fighter_2="Zoe Omega")

    assert classify(db, tmp_path, replaced) == "different opponent"


def test_the_pair_is_listed_but_without_a_price(db, tmp_path):
    add_fight(db, "Ann Alpha", "Bea Beta", date(2026, 2, 7))

    assert classify(db, tmp_path, feed_row(odds_1="", odds_2="")) == "in feed, no usable price"


# -- the consensus price ---------------------------------------------------------------------


def consensus_for(db, tmp_path, *rows):
    fight = ours(db)[0]
    feed = feed_of(tmp_path, *rows)
    _, matched = probe.match_fight(fight, feed)
    return probe.consensus(fight, matched)


def test_the_last_snapshot_per_bookmaker_on_or_before_the_event_is_used(db, tmp_path):
    add_fight(db, "Ann Alpha", "Bea Beta", date(2026, 2, 7))
    early = feed_row(odds_1="3.00", odds_2="1.40", adding_date="2026-02-01 00:00:00+00:00")
    late = feed_row(odds_1="1.50", odds_2="2.80", adding_date="2026-02-06 00:00:00+00:00")
    after_the_fight = feed_row(
        odds_1="9.00", odds_2="1.01", adding_date="2026-02-08 00:00:00+00:00"
    )

    c = consensus_for(db, tmp_path, early, late, after_the_fight)

    implied_1, implied_2 = 1 / 1.5, 1 / 2.8
    assert c.p_a == pytest.approx(implied_1 / (implied_1 + implied_2))
    assert c.bookmakers == 1 and c.days_before == 1


def test_each_bookmaker_is_oriented_to_our_fighter_a_before_averaging(db, tmp_path):
    """One book lists Ann first, another lists Bea first. Averaging P(fighter_1) as-is would
    cancel the two out; orienting each row first keeps them agreeing that Ann is the favorite."""
    add_fight(db, "Ann Alpha", "Bea Beta", date(2026, 2, 7))
    book_a = feed_row(source="BookA", fighter_1="Ann Alpha", fighter_2="Bea Beta",
                      odds_1="1.50", odds_2="2.80")  # fmt: skip
    book_b = feed_row(source="BookB", fighter_1="Bea Beta", fighter_2="Ann Alpha",
                      odds_1="2.80", odds_2="1.50")  # fmt: skip

    c = consensus_for(db, tmp_path, book_a, book_b)

    ann_is_a = ours(db)[0].norm_a == "ann alpha"
    expected_ann = (1 / 1.5) / (1 / 1.5 + 1 / 2.8)
    assert c.bookmakers == 2
    assert c.p_a == pytest.approx(expected_ann if ann_is_a else 1 - expected_ann)
    assert (c.p_a > 0.5) == ann_is_a


def test_rows_with_no_adding_date_are_read_and_then_ignored(db, tmp_path):
    add_fight(db, "Ann Alpha", "Bea Beta", date(2026, 2, 7))
    blank = feed_row(adding_date="", source="")

    assert consensus_for(db, tmp_path, blank) is None
    assert probe.parse_feed(write_csv(tmp_path / "g.csv", [blank]))[0].added is None


# -- the command ----------------------------------------------------------------------------


def run(db, tmp_path, *argv, rows=None):
    path = write_csv(tmp_path / "snap.csv", rows or [feed_row()])
    out = io.StringIO()
    with db.connect() as conn:
        code = probe.main([str(path), *argv], conn=conn, out=out)
    return code, out.getvalue(), hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_report_states_the_snapshot_hash_and_what_it_found(db, tmp_path):
    add_fight(db, "Ann Alpha", "Bea Beta", date(2026, 2, 7))

    code, out, digest = run(db, tmp_path)

    assert code == 0
    assert digest in out
    assert "labeled fights" in out and "test" in out


def test_the_expected_hash_is_enforced_before_anything_is_read(db, tmp_path, capsys):
    add_fight(db, "Ann Alpha", "Bea Beta", date(2026, 2, 7))

    code, out, digest = run(db, tmp_path, "--expect-sha256", "0" * 64)

    assert code == 1 and out == ""
    assert digest in capsys.readouterr().err
    assert run(db, tmp_path, "--expect-sha256", digest)[0] == 0


def test_a_missing_file_exits_one(tmp_path, capsys):
    assert probe.main([str(tmp_path / "nope.csv")]) == 1
    assert "does not exist" in capsys.readouterr().err


def test_the_json_report_is_machine_readable(db, tmp_path):
    import json

    add_fight(db, "Ann Alpha", "Bea Beta", date(2026, 2, 7))

    code, out, digest = run(db, tmp_path, "--json")
    report = json.loads(out)

    assert code == 0 and report["snapshot"]["sha256"] == digest
    assert report["coverage"]["test"]["fights"] >= 0
    assert report["our_database"]["labeled_fights"] == 1


def test_the_database_connection_is_read_only(db):
    add_fight(db, "Ann Alpha", "Bea Beta", date(2026, 2, 7))

    with db.connect() as conn:
        probe.load_our_fights(conn)
        with pytest.raises(DBAPIError, match="read-only"):
            conn.execute(text("UPDATE fighters SET name = 'x'"))
