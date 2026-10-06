import csv
from datetime import date
from decimal import Decimal

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import text

from cageops_worker.seed.odds import (
    MAX_RED_DISAGREEMENTS,
    FightRef,
    MdabbertRow,
    RedCornerDisagreementError,
    load_mdabbert,
    load_silver_odds,
    load_verified_disagreements,
    match_mdabbert,
    odds_summary,
    silver_odds_rows,
)
from cageops_worker.seed.resolver import NameResolver
from cageops_worker.seed.silver import load_silver

NAMES = {1: "Israel Adesanya", 2: "Robert Whittaker", 3: "Justin Gaethje", 4: "King Green"}
FIGHT = FightRef(
    fight_id=10, a=1, b=2, event_date=date(2019, 10, 5), red=2, winner=1, outcome="win"
)


def mrow(**overrides):
    base = {
        "red_name": "Robert Whittaker",
        "blue_name": "Israel Adesanya",
        "date": date(2019, 10, 5),
        "red_odds": 110.0,
        "blue_odds": -130.0,
        "winner": "Blue",
        "weight_class": "Middleweight",
    }
    return MdabbertRow(**(base | overrides))


def match(rows, fights=(FIGHT,), aliases=None):
    return match_mdabbert(list(rows), list(fights), NameResolver(NAMES, aliases))


def test_exact_date_match_aligns_odds_to_fighter_a_and_b_not_red_and_blue():
    result = match([mrow()])

    assert result.report["matched_exact_date"] == 1
    odds = result.odds[0]
    # fighter_a is Adesanya (blue, -130 -> 1.769), fighter_b is Whittaker (red, +110 -> 2.1)
    assert (odds["decimal_odds_a"], odds["decimal_odds_b"]) == (Decimal("1.769"), Decimal("2.1"))
    assert odds["source"] == "mdabbert" and odds["captured_at"] is None


def test_plus_minus_one_day_fallback_for_international_cards():
    result = match([mrow(date=date(2019, 10, 6))])

    assert result.report["matched_plus_minus_1_day"] == 1
    assert "matched_exact_date" not in result.report


def test_two_days_off_is_reported_not_matched():
    result = match([mrow(date=date(2019, 10, 7))])

    assert result.report["unmatched_no_fight"] == 1
    assert result.odds == []


def test_unresolved_name_is_reported_with_its_reason():
    result = match([mrow(red_name="Totally Unknown")])

    assert result.report["unmatched_name"] == 1
    assert result.report["unmatched_name_samples"] == [
        ("2019-10-05", "Totally Unknown", "unmatched")
    ]


def test_alias_lets_a_renamed_fighter_match():
    fights = [FightRef(11, 3, 4, date(2022, 2, 12), red=4, winner=4, outcome="win")]
    row = mrow(
        red_name="Bobby Green", blue_name="Justin Gaethje", date=date(2022, 2, 12), winner="Red"
    )

    result = match([row], fights, aliases={("mdabbert", "bobby green"): 4})

    assert result.report["matched_exact_date"] == 1


def test_red_corner_and_winner_disagreements_are_counted_and_listed():
    flipped = mrow(red_name="Israel Adesanya", blue_name="Robert Whittaker", winner="Blue")

    result = match([flipped])

    assert result.report["red_corner_disagree"] == 1
    assert result.report["winner_disagree"] == 1
    assert result.report["red_corner_disagreements"][0][1] == "Israel Adesanya"


def test_agreement_is_counted_when_sources_match():
    result = match([mrow()])

    assert result.report["red_corner_agree"] == 1
    assert result.report["winner_agree"] == 1


def test_draw_fills_an_unknown_outcome():
    fight = FightRef(10, 1, 2, date(2019, 10, 5), red=2, winner=None, outcome="unknown")

    result = match([mrow(winner="Draw")], [fight])

    assert result.outcome_updates == [{"id": 10, "outcome": "draw"}]


def test_matched_row_without_odds_is_counted_but_still_fixes_outcome():
    result = match([mrow(red_odds=None)])

    assert result.report["matched_without_odds"] == 1
    assert result.odds == []


def test_silver_odds_use_the_decimal_columns_for_each_flavor():
    fighter_ids = {"fa": 1, "fb": 2}
    fights = {"f1": (10, 1, 2), "f2": (11, 1, 2)}
    base = {"f_1_url": "x/fa", "f_2_url": "x/fb"}
    rows = [
        base
        | {
            "fight_url": "x/f1",
            "odds_source": "legacy",
            "f_1_odds_legacy": 1.5,
            "f_2_odds_legacy": 2.8,
        },
        base
        | {
            "fight_url": "x/f2",
            "odds_source": "bfo",
            "f_1_bfo_best_decimal": 3.0,
            "f_2_bfo_best_decimal": 1.4,
        },
        base | {"fight_url": "x/f3", "odds_source": None},
        base
        | {
            "fight_url": "x/f4",
            "odds_source": "bfo",
            "f_1_bfo_best_decimal": None,
            "f_2_bfo_best_decimal": 1.4,
        },
    ]

    odds, counts = silver_odds_rows(rows, fighter_ids, fights)

    assert [(o["fight_id"], o["source_detail"]) for o in odds] == [(10, "legacy"), (11, "bfo")]
    assert counts["no_odds_source"] == 1 and counts["unusable_decimal_odds"] == 1
    assert all(o["source"] == "silver" and o["captured_at"] is None for o in odds)


# ---- integration: real Postgres ----

MDABBERT_HEADER = ["R_fighter", "B_fighter", "R_odds", "B_odds", "date", "Winner", "weight_class"]


def write_mdabbert(tmp_path, rows):
    path = tmp_path / "ufc-master.csv"
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(MDABBERT_HEADER)
        writer.writerows(rows)
    return path


def silver_with_odds(tmp_path, silver_row, second_fight):
    rows = [
        silver_row(odds_source="legacy", f_1_odds_legacy=2.1, f_2_odds_legacy=1.769),
        silver_row(**second_fight)
        | {"odds_source": "bfo", "f_1_bfo_best_decimal": 1.3, "f_2_bfo_best_decimal": 3.6},
    ]
    columns = {name for row in rows for name in row}  # from_pylist keeps only the first row's keys
    path = tmp_path / "silver.parquet"
    pq.write_table(pa.Table.from_pylist([{c: r.get(c) for c in columns} for r in rows]), path)
    return path


GOOD_ROWS = [
    ["Robert Whittaker", "Israel Adesanya", "110", "-130", "2019-10-05", "Blue", "Middleweight"],
    ["Khabib Nurmagomedov", "Justin Gaethje", "-300", "250", "2020-10-24", "Red", "Lightweight"],
]


def test_odds_load_end_to_end_is_idempotent_and_reports_favorite_agreement(
    db, tmp_path, silver_row, second_fight
):
    silver = silver_with_odds(tmp_path, silver_row, second_fight)
    load_silver(db, silver, "s")
    load_silver_odds(db, silver, "s")
    mdabbert = write_mdabbert(tmp_path, GOOD_ROWS)

    first = load_mdabbert(db, mdabbert, "m", aliases_csv=None)
    load_mdabbert(db, mdabbert, "m", aliases_csv=None)
    summary = odds_summary(db)

    assert first["matched_exact_date"] == 2
    with db.connect() as conn:
        count = conn.execute(text("SELECT count(*) FROM odds")).scalar_one()
    assert count == 4  # 2 fights x 2 sources, no duplicates after the re-run
    assert summary["fights_with_mdabbert_odds"] == summary["fights_with_silver_odds"] == 2
    assert summary["silver_only_odds_fights"] == 0
    assert summary["favorite_agreement"]["all"] == {"fights": 2, "same_favorite": 2, "pct": 100.0}


def test_too_many_red_corner_disagreements_abort_without_writing(
    db, tmp_path, silver_row, second_fight
):
    silver = silver_with_odds(tmp_path, silver_row, second_fight)
    load_silver(db, silver, "s")
    # distinct fights whose corners are flipped in mdabbert, more than the allowed limit
    fights = []
    for i in range(MAX_RED_DISAGREEMENTS + 1):
        fights.append(
            silver_row(
                fight_url=f"http://ufcstats.com/fight-details/{i:016x}",
                f_1_url=f"http://ufcstats.com/fighter-details/a{i:015x}",
                f_1_name=f"Red {i}",
                f_2_url=f"http://ufcstats.com/fighter-details/b{i:015x}",
                f_2_name=f"Blue {i}",
                winner=f"Red {i}",
                event_date=date(2021, 1, 1),
            )
        )
    path = tmp_path / "many.parquet"
    pq.write_table(pa.Table.from_pylist(fights), path)
    load_silver(db, path, "many")
    flipped = [
        [f"Blue {i}", f"Red {i}", "100", "-120", "2021-01-01", "Blue", "Middleweight"]
        for i in range(MAX_RED_DISAGREEMENTS + 1)
    ]

    with pytest.raises(RedCornerDisagreementError) as caught:
        load_mdabbert(db, write_mdabbert(tmp_path, flipped), "m", aliases_csv=None)

    assert caught.value.report["red_corner_disagree"] == MAX_RED_DISAGREEMENTS + 1
    with db.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM odds")).scalar_one() == 0
        assert (
            conn.execute(
                text("SELECT count(*) FROM load_runs WHERE source = 'mdabbert'")
            ).scalar_one()
            == 0
        )


def test_verified_disagreements_are_listed_as_mdabbert_errors_in_the_report(tmp_path):
    csv_path = tmp_path / "verified.csv"
    csv_path.write_text(
        "event_date,fighter_1,fighter_2,verdict,evidence\n"
        '2019-10-05,Israel Adesanya,Robert Whittaker,mdabbert wrong,"checked on ufcstats"\n'
    )
    flipped = mrow(red_name="Israel Adesanya", blue_name="Robert Whittaker", winner="Blue")
    other = mrow(
        date=date(2022, 2, 12), red_name="Bobby Green", blue_name="Justin Gaethje", winner="Red"
    )
    fights = [FIGHT, FightRef(11, 3, 4, date(2022, 2, 12), red=3, winner=3, outcome="win")]

    result = match_mdabbert(
        [flipped, other],
        fights,
        NameResolver(NAMES, {("mdabbert", "bobby green"): 4}),
        load_verified_disagreements(csv_path),
    )

    errors = result.report["verified_mdabbert_errors"]
    assert len(errors) == 1
    assert errors[0]["verdict"] == "mdabbert wrong"
    assert errors[0]["fields"] == ["red_corner", "winner"]
    assert errors[0]["evidence"] == "checked on ufcstats"
    # the unverified disagreement (Green/Gaethje corners) is still in the raw list, not "verified"
    assert result.report["red_corner_disagree"] == 2


def test_committed_verified_disagreements_file_is_well_formed():
    entries = load_verified_disagreements()

    assert len(entries) == 2
    assert all(e["verdict"] == "mdabbert wrong" and e["evidence"] for e in entries.values())


def test_odds_loading_still_works_when_another_sources_rows_exist(
    db, tmp_path, silver_row, second_fight
):
    """Smoke test (D-029): the id maps skip NULL-id rows. The filter is defensive: a None key in
    those dicts is harmless, so this test cannot fail without it."""
    silver = silver_with_odds(tmp_path, silver_row, second_fight)
    load_silver(db, silver, "s")
    with db.begin() as conn:
        conn.execute(text("INSERT INTO fighters (wikipedia_title, name) VALUES ('X_Y', 'X Y')"))
        conn.execute(text("INSERT INTO fighters (wikipedia_title, name) VALUES ('Z_W', 'Z W')"))
    before = load_silver_odds(db, silver, "s")

    again = load_silver_odds(db, silver, "s")

    assert again == before
    with db.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM odds")).scalar_one() > 0
