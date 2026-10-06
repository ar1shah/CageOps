"""The feature maths, with no database. Every expected number is worked out by hand."""

from datetime import date

import pytest

from cageops_worker.features.build import FEATURE_COLUMNS
from cageops_worker.features.compute import (
    PriorFight,
    Stats,
    Subject,
    compute_features,
    fight_duration,
    win_streak,
)
from cageops_worker.features.history import FightRecord, History, prior_fights


def stats(**kw) -> Stats:
    base = dict(sig_landed=None, sig_att=None, td_landed=None, td_att=None, sub_att=None)
    return Stats(**(base | dict(knockdowns=None) | kw))


def prior(day=1, result="win", method="decision", duration=900, own=None, opp=None, fight_id=None):
    return PriorFight(
        fight_id=fight_id or day,
        event_date=date(2020, 1, day),
        result=result,
        method=method,
        duration_sec=duration,
        own=own,
        opp=opp,
    )


def subject(**kw) -> Subject:
    base = dict(
        event_date=date(2021, 1, 1),
        dob=None,
        height_cm=None,
        reach_cm=None,
        stance=None,
        weight_class="Lightweight",
        is_title_fight=False,
        scheduled_rounds=3,
    )
    return Subject(**(base | kw))


def features(fights, **kw):
    return compute_features(fights, subject(**kw))


# -- the output matches the table ------------------------------------------------------------


def test_a_feature_row_has_exactly_the_table_columns():
    keys = set(features([])) | {"fight_id", "fighter_id"}
    assert keys == set(FEATURE_COLUMNS)


# -- rates: ratio of sums, NULL not zero -------------------------------------------------------


def test_rates_are_a_ratio_of_sums_not_a_mean_of_ratios():
    # Fight 1: 30 strikes in 15 min (2.0/min). Fight 2: 10 strikes in 100 s (6.0/min).
    # Mean of ratios would be 4.0; ratio of sums is 40 / (15 + 100/60) = 2.4.
    fights = [
        prior(1, own=stats(sig_landed=30, sig_att=60), duration=900),
        prior(2, own=stats(sig_landed=10, sig_att=10), duration=100),
    ]

    row = features(fights)

    assert row["sig_str_landed_pm_career"] == pytest.approx(2.4)
    assert row["sig_str_acc_career"] == pytest.approx(40 / 70)


def test_absorbed_and_defense_come_from_the_opponents_numbers():
    fights = [prior(1, own=stats(), opp=stats(sig_landed=20, sig_att=50, td_landed=1, td_att=4))]

    row = features(fights)

    assert row["sig_str_absorbed_pm_career"] == pytest.approx(20 / 15)  # 15 minutes
    assert row["sig_str_def_career"] == pytest.approx(1 - 20 / 50)
    assert row["td_def_career"] == pytest.approx(1 - 1 / 4)


def test_per_15_rates_use_fifteen_minute_units():
    fights = [prior(1, own=stats(td_landed=3, sub_att=2, knockdowns=1), duration=450)]

    row = features(fights)

    assert row["td_landed_per15_career"] == pytest.approx(3 / 0.5)  # 450 s = half of 15 min
    assert row["sub_att_per15_career"] == pytest.approx(2 / 0.5)
    assert row["kd_per15_career"] == pytest.approx(1 / 0.5)


def test_windows_take_the_most_recent_fights():
    fights = [prior(d, own=stats(sig_landed=d * 15), duration=900) for d in range(1, 8)]

    row = features(fights)

    assert row["n_fights_career"] == 7
    assert (row["n_fights_last3"], row["n_fights_last5"]) == (3, 5)
    assert row["sig_str_landed_pm_last3"] == pytest.approx((5 + 6 + 7) * 15 / 45)
    assert row["sig_str_landed_pm_last5"] == pytest.approx((3 + 4 + 5 + 6 + 7) * 15 / 75)


def test_missing_stats_are_null_not_zero_and_are_counted():
    fights = [prior(1, own=None, opp=None), prior(2, own=stats(sig_landed=15, sig_att=30))]

    row = features(fights)

    assert row["sig_str_landed_pm_career"] == pytest.approx(1.0)  # only the fight with stats
    assert row["n_fights_career"] == 2
    assert row["n_fights_with_stats_career"] == 0  # needs both fighters' rows
    assert features([prior(1)])["sig_str_landed_pm_career"] is None


def test_a_fight_without_a_trusted_duration_is_skipped_for_time_rates_but_still_counted():
    fights = [
        prior(1, own=stats(sig_landed=30, sig_att=60), duration=900),
        prior(2, own=stats(sig_landed=90, sig_att=90), duration=None),
    ]

    row = features(fights)

    assert row["sig_str_landed_pm_career"] == pytest.approx(2.0)  # fight 2 left out of minutes
    assert row["sig_str_acc_career"] == pytest.approx(120 / 150)  # accuracy needs no duration
    assert (row["n_fights_career"], row["n_fights_with_duration_career"]) == (2, 1)


def test_no_history_means_zero_counts_and_null_rates():
    row = features([])

    assert row["prior_ufc_fights"] == 0
    assert row["win_streak"] == 0
    assert row["days_since_last_fight"] is None
    assert row["finish_rate"] is None
    assert row["sig_str_landed_pm_career"] is None
    assert row["n_fights_career"] == 0


# -- zero denominators are NULL (#6) -------------------------------------------------------


def test_zero_opponent_strike_attempts_make_sig_str_def_null():
    row = features([prior(1, opp=stats(sig_landed=0, sig_att=0), own=stats())])
    assert row["sig_str_def_career"] is None


def test_zero_opponent_takedown_attempts_make_td_def_null():
    row = features([prior(1, opp=stats(td_landed=0, td_att=0), own=stats())])
    assert row["td_def_career"] is None


def test_zero_own_attempts_make_accuracy_null():
    row = features([prior(1, own=stats(sig_landed=0, sig_att=0))])
    assert row["sig_str_acc_career"] is None


def test_zero_total_minutes_make_the_time_rates_null():
    row = features(
        [prior(1, duration=0, own=stats(sig_landed=5, td_landed=1, sub_att=1, knockdowns=1))]
    )
    for rate in ("sig_str_landed_pm", "td_landed_per15", "sub_att_per15", "kd_per15"):
        assert row[f"{rate}_career"] is None
    assert row["sig_str_absorbed_pm_career"] is None


def test_a_real_zero_stays_zero():
    # 0 landed out of 10 attempts is a real zero, and 0 over a positive time is 0.0, not NULL.
    row = features([prior(1, own=stats(sig_landed=0, sig_att=10, td_landed=0, knockdowns=0))])
    assert row["sig_str_acc_career"] == 0.0
    assert row["td_landed_per15_career"] == 0.0


# -- win streak (#5) -----------------------------------------------------------------------


def streak(*results):
    return win_streak([prior(i + 1, result=r) for i, r in enumerate(results)])


def test_wins_extend_the_streak_and_a_loss_ends_it():
    assert streak("loss", "win", "win") == 2
    assert streak("win", "win", "loss") == 0


def test_a_draw_ends_the_streak():
    assert streak("win", "win", "draw", "win") == 1
    assert streak("win", "win", "draw") == 0


def test_a_no_contest_is_skipped():
    assert streak("win", "win", "no_contest", "win") == 3  # neither extends nor ends it
    assert streak("loss", "no_contest") == 0
    assert streak("no_contest") == 0


def test_an_unknown_result_we_reach_makes_the_streak_null():
    assert streak("win", "unknown", "win") is None
    assert streak("unknown") is None


def test_an_unknown_behind_the_end_of_the_streak_does_not_matter():
    assert streak("unknown", "loss", "win") == 1
    assert streak("unknown", "draw", "win") == 1


def test_no_prior_fights_is_a_streak_of_zero():
    assert win_streak([]) == 0


# -- other scalars ---------------------------------------------------------------------------


def test_finish_rate_counts_finishing_wins_over_decided_fights():
    fights = [
        prior(1, "win", "ko_tko"),
        prior(2, "win", "decision"),
        prior(3, "loss", "submission"),  # a finish, but a loss
        prior(4, "draw", "decision"),  # not decided
        prior(5, "no_contest", "other"),
        prior(6, "unknown", "decision"),
    ]
    assert features(fights)["finish_rate"] == pytest.approx(1 / 3)


def test_prior_ufc_fights_counts_every_prior_fight_whatever_the_result():
    fights = [prior(1, "win"), prior(2, "draw"), prior(3, "no_contest"), prior(4, "unknown")]
    assert features(fights)["prior_ufc_fights"] == 4


def test_days_since_last_fight_and_age():
    row = features([prior(1), prior(10)], dob=date(2000, 1, 1))  # fights 2020-01-01, 2020-01-10
    assert row["days_since_last_fight"] == (date(2021, 1, 1) - date(2020, 1, 10)).days
    assert row["age_days"] == (date(2021, 1, 1) - date(2000, 1, 1)).days
    assert features([prior(1)])["age_days"] is None


# -- duration (#1) ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rounds", "finish_round", "finish_time", "method", "expected"),
    [
        (3, 3, 300, "decision", (900, None)),
        (5, 5, 300, "decision", (1500, None)),
        (3, 2, 138, "ko_tko", (438, None)),  # 300 for round 1 plus 138
        (5, 1, 12, "submission", (12, None)),
        (None, 1, 100, "ko_tko", (None, "format_not_standard")),  # "no time limit" or unknown
        (1, 1, 300, "ko_tko", (None, "format_not_standard")),
        (2, 2, 300, "decision", (None, "format_not_standard")),
        (3, 4, 100, "ko_tko", (None, "finish_round_out_of_range")),  # overtime
        (3, None, 100, "ko_tko", (None, "finish_round_out_of_range")),
        (3, 2, 301, "ko_tko", (None, "finish_time_out_of_range")),
        (3, 2, 0, "ko_tko", (None, "finish_time_out_of_range")),
        (3, 2, None, "ko_tko", (None, "finish_time_out_of_range")),
        (3, 3, 115, "decision", (None, "decision_not_full_length")),  # e.g. a technical decision
        (5, 3, 300, "decision", (None, "decision_not_full_length")),
    ],
)
def test_fight_duration(rounds, finish_round, finish_time, method, expected):
    assert fight_duration(rounds, finish_round, finish_time, method) == expected


# -- ordering (#3): same-date fights, and the strict filter ------------------------------------


def record(fight_id, day) -> FightRecord:
    return FightRecord(
        fight_id=fight_id,
        event_date=day,
        fighter_a_id=1,
        fighter_b_id=100 + fight_id,
        winner_id=1,
        outcome="win",
        method="decision",
        scheduled_rounds=3,
        finish_round=3,
        finish_time_sec=300,
        weight_class="Lightweight",
        is_title_fight=False,
        duration_sec=900,
        duration_null_reason=None,
    )


def test_history_is_ordered_by_date_then_fight_id_whatever_order_it_was_loaded_in():
    same_day = date(2020, 6, 1)
    fights = [
        record(7, same_day),
        record(3, same_day),
        record(5, same_day),
        record(9, date(2020, 1, 1)),
    ]

    for order in (fights, fights[::-1], [fights[2], fights[0], fights[3], fights[1]]):
        history = History(completed=order, scheduled=[], bios={}, totals={})
        got = prior_fights(history, 1, before=date(2021, 1, 1))
        assert [f.fight_id for f in got] == [9, 3, 5, 7]


def test_prior_fights_is_strictly_before():
    history = History(
        completed=[
            record(1, date(2020, 1, 1)),
            record(2, date(2020, 1, 2)),
            record(3, date(2020, 1, 3)),
        ],
        scheduled=[],
        bios={},
        totals={},
    )

    assert [f.fight_id for f in prior_fights(history, 1, before=date(2020, 1, 2))] == [1]
    assert [f.fight_id for f in prior_fights(history, 1, before=date(2020, 1, 1))] == []
