"""Shared fixtures for the seed-loader tests."""

from datetime import date

import pytest


def make_silver_row(**overrides):
    """A sparse silver-style row: Whittaker (slot 1, red) vs Adesanya (slot 2), UFC 243."""
    row = {
        "fight_url": "http://ufcstats.com/fight-details/ffff000000000001",
        "event_url": "http://ufcstats.com/event-details/eeee000000000001",
        "event_name": "UFC 243: Whittaker vs. Adesanya",
        "event_date": date(2019, 10, 5),
        "event_city": "Melbourne",
        "event_state": "NULL",  # silver's literal-string null
        "event_country": "Australia",
        "f_1_url": "http://ufcstats.com/fighter-details/dddd000000000001",
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


@pytest.fixture
def silver_row():
    """Factory for sparse silver-style rows (Whittaker vs Adesanya by default)."""
    return make_silver_row


@pytest.fixture
def second_fight():
    """Overrides that turn a silver row into Khabib vs Gaethje with no stats at all, like the
    earliest UFC cards."""
    no_stats = {k: None for k in make_silver_row() if "sig_strikes" in k or "knockdowns" in k}
    return no_stats | {
        "fight_url": "http://ufcstats.com/fight-details/ffff000000000002",
        "event_url": "http://ufcstats.com/event-details/eeee000000000002",
        "event_name": "UFC 254",
        "event_date": date(2020, 10, 24),
        "f_1_url": "http://ufcstats.com/fighter-details/cccc000000000003",
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
