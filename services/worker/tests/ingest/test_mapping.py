from datetime import date

import pytest

from cageops_scraper.parsers.models import FightFighter
from cageops_worker.ingest.errors import MappingError
from cageops_worker.ingest.mapping import (
    gender_for,
    map_event,
    map_fight,
    map_fighter,
    map_scheduled_bout,
    split_bouts,
)
from cageops_worker.seed.normalize import UnknownValueError

BURNS, MALOTT = "23024fdfc966410a", "dd6103dd7127db1d"

# What the Phase 1a seed holds for each fixture fight (read from the real database; the two
# fields that differ on purpose are marked). Columns only: ids are checked separately.
SEED = {
    "32054bf2b36b0e47": dict(  # Burns vs Malott
        weight_class="Welterweight", gender="M", is_title_fight=False, scheduled_rounds=5,
        outcome="win", method="ko_tko", decision_type=None,
        method_detail="Punches to Head On Ground", finish_round=3, finish_time_sec=128,
        referee="Herb Dean", has_round_stats=True,
    ),
    "b5299e5b946015e5": dict(  # Phillips vs Jourdain
        weight_class="Bantamweight", gender="M", is_title_fight=False, scheduled_rounds=3,
        outcome="win", method="decision", decision_type="unanimous",
        method_detail="Junichiro Kamijo 28 - 29. Sal D'amato 28 - 29. Jason Rodgers 28 - 29.",
        finish_round=3, finish_time_sec=300, referee="Jerin Valel", has_round_stats=True,
    ),
    "9fab4b0ad082f670": dict(  # Valentin vs Leblanc
        weight_class="Middleweight", gender="M", is_title_fight=False, scheduled_rounds=3,
        outcome="win", method="submission", decision_type=None, method_detail="Rear Naked Choke",
        finish_round=1, finish_time_sec=142, referee="Chris Desautels", has_round_stats=True,
    ),
    "552f7cdaf93e1055": dict(  # Vologdin vs Castaneda: the seed says outcome 'unknown'
        weight_class="Catch Weight", gender="M", is_title_fight=False, scheduled_rounds=3,
        outcome="draw", method="decision", decision_type="majority",
        method_detail="Laura Baldwin 27 - 29. Jason Rodgers 28 - 28. Mike Bell 28 - 28.",
        finish_round=3, finish_time_sec=300, referee="Jason Herzog", has_round_stats=True,
    ),
    "6390c8e74630473e": dict(  # Aspinall vs Gane
        weight_class="Heavyweight", gender="M", is_title_fight=True, scheduled_rounds=5,
        outcome="no_contest", method="other", decision_type=None,
        method_detail="Eye poke by Gane", finish_round=1, finish_time_sec=275,
        referee="Jason Herzog", has_round_stats=True,
    ),
    "635fbf57001897c7": dict(  # Santos vs Marscucci: the seed says finish_time_sec 27
        weight_class="Lightweight", gender="M", is_title_fight=False, scheduled_rounds=1,
        outcome="win", method="ko_tko", decision_type=None, method_detail="to",
        finish_round=1, finish_time_sec=627, referee="John McCarthy", has_round_stats=False,
    ),
}  # fmt: skip
# The red corner (fighter_a/red in the seed): the first fighter on the fight page.
SEED_RED = {
    "32054bf2b36b0e47": BURNS,
    "b5299e5b946015e5": "60425d07ef4b91a7",
    "9fab4b0ad082f670": "cd0dfd9846bd03b9",
    "552f7cdaf93e1055": "5fa2974cbd18e05c",
    "6390c8e74630473e": "399afbabc02376b5",
    "635fbf57001897c7": "e8efeb9cf33b1941",
}
COLUMNS = list(SEED["32054bf2b36b0e47"])


# -- a fight that happened -------------------------------------------------------------


@pytest.mark.parametrize("fight_id", SEED)
def test_every_fixture_fight_maps_to_exactly_the_seeds_columns(fight, bout_for, fight_id):
    mapped = map_fight(fight(fight_id), bout_for(fight_id))

    assert {c: mapped.fight[c] for c in COLUMNS} == SEED[fight_id]
    assert mapped.fight["status"] == "completed"
    assert mapped.fight["red_ufcstats_id"] == SEED_RED[fight_id]


def test_the_winner_comes_from_the_badges_not_from_the_order(fight):
    page = fight("32054bf2b36b0e47")  # Burns (L) listed first, Malott (W) second

    mapped = map_fight(page)

    assert mapped.fight["winner_ufcstats_id"] == MALOTT
    assert mapped.fight["red_ufcstats_id"] == BURNS  # first on the fight page is red
    assert mapped.fight["participants"] == [BURNS, MALOTT]


def test_draws_and_no_contests_have_no_winner(fight):
    assert map_fight(fight("552f7cdaf93e1055")).fight["winner_ufcstats_id"] is None
    assert map_fight(fight("6390c8e74630473e")).fight["winner_ufcstats_id"] is None


@pytest.mark.parametrize(
    "badges",
    [("W", "W"), ("L", "L"), ("D", "W"), ("NC", "L"), ("D", "NC")],
)
def test_badges_that_dont_describe_one_result_raise(fight, badges):
    page = fight("32054bf2b36b0e47")
    fighters = tuple(
        FightFighter(ufcstats_id=f.ufcstats_id, name=f.name, result=b)
        for f, b in zip(page.fighters, badges, strict=True)
    )

    with pytest.raises(MappingError, match="don't describe one result"):
        map_fight(page.model_copy(update={"fighters": fighters}))


def test_swapping_the_order_swaps_red_but_not_the_winner(fight):
    page = fight("32054bf2b36b0e47")
    swapped = page.model_copy(update={"fighters": (page.fighters[1], page.fighters[0])})

    mapped = map_fight(swapped)

    assert mapped.fight["winner_ufcstats_id"] == MALOTT
    assert mapped.fight["red_ufcstats_id"] == MALOTT


def test_an_unknown_method_raises_like_the_seed_loader_does(fight):
    page = fight("32054bf2b36b0e47").model_copy(update={"method_raw": "Magic"})

    with pytest.raises(UnknownValueError, match="unknown fight result"):
        map_fight(page)


# -- weight class and gender -----------------------------------------------------------


def test_the_event_pages_plain_division_name_is_used_when_there_is_one(fight, bout_for):
    mapped = map_fight(fight("6390c8e74630473e"))  # not on the card: falls back to the title

    assert mapped.fight["weight_class"] == "Heavyweight"
    assert mapped.fight["is_title_fight"] is True  # the title flag comes from the fight page


def test_a_disagreement_between_event_and_fight_page_is_an_anomaly_and_the_event_name_wins(
    fight, bout_for
):
    bout = bout_for("32054bf2b36b0e47").model_copy(update={"weight_class_raw": "Lightweight"})

    mapped = map_fight(fight("32054bf2b36b0e47"), bout)

    assert mapped.fight["weight_class"] == "Lightweight"
    assert "weight_class_mismatch:Lightweight!=Welterweight" in mapped.anomalies


def test_an_unknown_division_on_a_completed_fight_raises(fight, bout_for):
    bout = bout_for("32054bf2b36b0e47").model_copy(update={"weight_class_raw": "Superheavyweight"})

    with pytest.raises(UnknownValueError, match="unknown weight class"):
        map_fight(fight("32054bf2b36b0e47"), bout)


def test_an_unknown_title_and_no_event_name_raises(fight):
    page = fight("32054bf2b36b0e47").model_copy(update={"bout_title_raw": "Strange Bout"})

    with pytest.raises(UnknownValueError):
        map_fight(page)


@pytest.mark.parametrize(
    ("weight_class", "gender", "guessed"),
    [
        ("Lightweight", "M", False),
        ("Women's Strawweight", "F", False),
        ("Women's Bantamweight", "F", False),
        ("Catch Weight", "M", True),  # a women's catch-weight bout would also say M
        ("Open Weight", "M", True),
        (None, "M", True),
    ],
)
def test_gender_comes_from_the_division_and_says_when_it_is_a_guess(weight_class, gender, guessed):
    assert gender_for(weight_class) == (gender, guessed)


def test_a_catch_weight_fight_records_the_gender_guess_as_an_anomaly(fight, bout_for):
    mapped = map_fight(fight("552f7cdaf93e1055"), bout_for("552f7cdaf93e1055"))

    assert mapped.fight["gender"] == "M"
    assert mapped.anomalies == ["gender_assumed:Catch Weight"]


# -- the event page vs the fight page --------------------------------------------------


def test_matching_event_and_fight_pages_have_no_anomalies(fight, bout_for):
    for fight_id in ("32054bf2b36b0e47", "b5299e5b946015e5", "9fab4b0ad082f670"):
        assert map_fight(fight(fight_id), bout_for(fight_id)).anomalies == []


def test_an_event_page_that_names_a_different_winner_is_an_anomaly_and_the_fight_page_wins(
    fight, bout_for
):
    bout = bout_for("32054bf2b36b0e47").model_copy(update={"winner_id": BURNS})

    mapped = map_fight(fight("32054bf2b36b0e47"), bout)

    assert mapped.anomalies == ["event_fight_result_mismatch:fight=32054bf2b36b0e47"]
    assert mapped.fight["winner_ufcstats_id"] == MALOTT


# -- stats -----------------------------------------------------------------------------


def test_totals_hold_the_core_stats_only(fight):
    mapped = map_fight(fight("32054bf2b36b0e47"))
    burns = next(t for t in mapped.totals if t["fighter_ufcstats_id"] == BURNS)

    assert len(mapped.totals) == 2
    assert burns == {
        "fighter_ufcstats_id": BURNS,
        "knockdowns": 0,
        "sig_strikes_landed": 40,
        "sig_strikes_att": 92,
        "total_strikes_landed": 42,
        "total_strikes_att": 94,
        "takedowns_landed": 0,
        "takedowns_att": 7,
        "submission_att": 0,
        "reversals": 0,
        "ctrl_sec": 1,
    }  # no head/body/leg: fight_totals has no such columns


def test_round_rows_carry_the_round_and_the_strike_breakdown(fight):
    mapped = map_fight(fight("32054bf2b36b0e47"))
    r3 = next(r for r in mapped.rounds if r["fighter_ufcstats_id"] == BURNS and r["round"] == 3)

    assert len(mapped.rounds) == 6
    assert (r3["sig_strikes_landed"], r3["sig_strikes_att"]) == (6, 17)
    assert (r3["head_landed"], r3["head_att"], r3["ground_att"]) == (2, 11, 0)
    assert {"round", "fighter_ufcstats_id", "ctrl_sec", "distance_landed"} <= set(r3)


def test_a_fight_with_no_stats_gives_empty_lists_not_zero_rows(fight):
    mapped = map_fight(fight("635fbf57001897c7"))

    assert mapped.totals == [] and mapped.rounds == []
    assert mapped.fight["has_round_stats"] is False


def test_page_anomalies_are_passed_through(fight):
    page = fight("32054bf2b36b0e47").model_copy(
        update={"anomalies": ["unrecognized_time_format:x"]}
    )

    assert "unrecognized_time_format:x" in map_fight(page).anomalies


# -- events and fighters ---------------------------------------------------------------


def test_the_event_row_matches_the_seeds(burns_card):
    assert map_event(burns_card) == {
        "ufcstats_id": "c3ac8d0da7b05772",
        "name": "UFC Fight Night: Burns vs. Malott",
        "event_date": date(2026, 4, 18),
        "city": "Winnipeg",
        "state": "Manitoba",
        "country": "Canada",
    }


def test_the_fighter_row_has_only_bio_columns(fighter):
    burns = map_fighter(fighter(BURNS))

    assert burns == {
        "ufcstats_id": BURNS,
        "name": "Gilbert Burns",
        "dob": date(1986, 7, 20),
        "height_cm": 177.8,
        "reach_cm": 180.34,
        "stance": "Orthodox",
    }


def test_an_empty_bio_maps_to_nulls(fighter):
    nilson = map_fighter(fighter("53e533db1b8e9712"))

    assert (nilson["dob"], nilson["height_cm"], nilson["reach_cm"], nilson["stance"]) == (
        None,
        None,
        None,
        None,
    )


# -- scheduled bouts -------------------------------------------------------------------


def test_bouts_split_by_whether_they_have_a_result(burns_card, upcoming_card):
    played, unplayed = split_bouts(burns_card)
    assert (len(played), len(unplayed)) == (12, 0)
    played, unplayed = split_bouts(upcoming_card)
    assert (len(played), len(unplayed)) == (0, 12)


def test_a_scheduled_bout_has_no_result_and_unknown_title_and_rounds(upcoming_card):
    bout = upcoming_card.bouts[0]  # Allen vs Duncan, Middleweight

    mapped = map_scheduled_bout(bout, upcoming_card.ufcstats_id)

    assert mapped.fight == {
        "ufcstats_id": "7db1a3dac7e343e7",
        "event_ufcstats_id": "7f98d9d5a10fa25c",
        "participants": ["2f181c0467965b98", "a93f94c923c3a9cb"],
        "red_ufcstats_id": None,
        "winner_ufcstats_id": None,
        "status": "scheduled",
        "weight_class": "Middleweight",
        "gender": "M",
        "is_title_fight": None,  # unknown, not False
        "scheduled_rounds": None,
        "outcome": None,
        "method": None,
        "decision_type": None,
        "method_detail": None,
        "finish_round": None,
        "finish_time_sec": None,
        "referee": None,
        "has_round_stats": False,
    }
    assert mapped.anomalies == []
    assert [f["name"] for f in mapped.fighters] == ["Brendan Allen", "Christian Leroy Duncan"]


def test_a_womens_scheduled_bout_is_f(upcoming_card):
    bout = next(b for b in upcoming_card.bouts if b.weight_class_raw == "Women's Strawweight")

    assert map_scheduled_bout(bout, upcoming_card.ufcstats_id).fight["gender"] == "F"


def test_an_odd_weight_class_on_a_scheduled_bout_is_null_with_an_anomaly_not_a_failure(
    upcoming_card,
):
    bout = upcoming_card.bouts[0].model_copy(update={"weight_class_raw": "Superheavyweight"})

    mapped = map_scheduled_bout(bout, upcoming_card.ufcstats_id)

    assert mapped.fight["weight_class"] is None
    assert "unknown_weight_class:Superheavyweight" in mapped.anomalies
    assert any(a.startswith("gender_assumed") for a in mapped.anomalies)
