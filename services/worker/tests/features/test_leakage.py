"""Rule 1: a fight's features may only use data from strictly before that fight's date.

Each test changes the data a feature must NOT depend on and asserts the features stay put. The
mutation test also asserts that the change DID move some later rows, so it can't pass by being
blind. Comparisons use FEATURE_COLUMNS: every column except built_at and load_run_id.
"""

from datetime import date, timedelta

from cageops_worker.features.build import BOOKKEEPING_COLUMNS, FEATURE_COLUMNS

START = date(2020, 1, 1)
ROW = {"sig_landed": 40, "sig_att": 80, "td_landed": 1, "td_att": 3, "sub_att": 1, "knockdowns": 0}


def a_career(world):
    """Five fighters, eight fights over about two years, with stats on both sides of each fight.
    Returns the fight ids in date order."""
    for fighter_id in range(1, 6):
        world.fighter(fighter_id, dob=date(1990, 1, 1), height_cm=180.0, reach_cm=183.0)
    matchups = [(1, 2), (3, 4), (1, 3), (2, 5), (1, 4), (3, 5), (1, 5), (2, 4)]
    fights = []
    for i, (a, b) in enumerate(matchups):
        fights.append(
            world.fight(
                a,
                b,
                START + timedelta(days=90 * i),
                winner=a if i % 2 == 0 else b,
                method="decision" if i % 3 else "ko_tko",
                finish_round=3 if i % 3 else 1,
                finish_time=300 if i % 3 else 150,
                stats={
                    a: ROW | {"sig_landed": 30 + 5 * i},
                    b: ROW | {"sig_landed": 50 - 3 * i},
                },
            )
        )
    return fights


def test_feature_columns_exclude_exactly_the_bookkeeping_columns(world):
    assert BOOKKEEPING_COLUMNS == ("built_at", "load_run_id")
    assert not set(BOOKKEEPING_COLUMNS) & set(FEATURE_COLUMNS)
    assert {"fight_id", "fighter_id", "prior_ufc_fights", "win_streak"} <= set(FEATURE_COLUMNS)


def test_rewriting_a_fight_and_everything_after_it_cannot_change_features_up_to_it(world):
    fights = a_career(world)
    cutoff = fights[3]
    cutoff_date = world.fight_date(cutoff)
    world.build()
    before = world.features()

    # Rewrite the result and every stat of the cutoff fight and of every fight after it.
    world.sql(
        "UPDATE fight_totals SET sig_strikes_landed = sig_strikes_landed * 3 + 7,"
        " takedowns_landed = 9, takedowns_att = 9, knockdowns = 4, submission_att = 8"
        " WHERE fight_id IN (SELECT f.id FROM fights f JOIN events e ON e.id = f.event_id"
        "                    WHERE e.event_date >= :d)",
        d=cutoff_date,
    )
    world.sql(
        "UPDATE fights SET winner_id = CASE WHEN winner_id = fighter_a_id THEN fighter_b_id"
        " ELSE fighter_a_id END, method = 'submission', finish_round = 1, finish_time_sec = 100"
        " WHERE id IN (SELECT f.id FROM fights f JOIN events e ON e.id = f.event_id"
        "              WHERE e.event_date >= :d)",
        d=cutoff_date,
    )
    world.build()
    after = world.features()

    assert after.keys() == before.keys()
    up_to_cutoff = [k for k in before if world.fight_date(k[0]) <= cutoff_date]
    assert up_to_cutoff
    for key in up_to_cutoff:
        assert after[key] == before[key], f"features moved for fight/fighter {key}"
    later = [k for k in before if world.fight_date(k[0]) > cutoff_date]
    assert any(after[k] != before[k] for k in later), "the rewrite changed nothing downstream"


def test_a_fights_own_stats_are_not_in_its_own_features(world):
    fights = a_career(world)
    target = fights[4]
    world.build()
    before = world.features()

    world.sql("DELETE FROM fight_totals WHERE fight_id = :f", f=target)
    world.build()

    after = world.features()
    assert {k: v for k, v in after.items() if k[0] == target} == {
        k: v for k, v in before.items() if k[0] == target
    }


def test_a_fight_on_the_same_date_is_not_history_and_the_day_before_is(world):
    for fighter_id in (1, 2, 3, 4, 5, 6):
        world.fighter(fighter_id)
    day = date(2021, 6, 12)
    world.fight(1, 2, day - timedelta(days=1), winner=1, stats={1: ROW, 2: ROW})
    target = world.fight(1, 3, day, winner=1, stats={1: ROW, 3: ROW})
    same_day = world.fight(1, 4, day, winner=4, stats={1: ROW, 4: ROW})  # another event, same date
    next_day = world.fight(1, 5, day + timedelta(days=1), winner=1, stats={1: ROW, 5: ROW})

    world.build()
    rows = world.features()

    assert rows[(target, 1)]["prior_ufc_fights"] == 1  # the day before counts, the same day doesn't
    assert rows[(same_day, 1)]["prior_ufc_fights"] == 1
    assert rows[(target, 1)]["days_since_last_fight"] == 1
    assert rows[(next_day, 1)]["prior_ufc_fights"] == 3  # both same-day fights are history now


def test_moving_a_later_fight_onto_fight_fs_date_does_not_change_fs_features(world):
    for fighter_id in (1, 2, 3, 4):
        world.fighter(fighter_id)
    world.fight(1, 2, date(2021, 1, 1), winner=1, stats={1: ROW, 2: ROW})
    target = world.fight(1, 3, date(2021, 6, 1), winner=3, stats={1: ROW, 3: ROW})
    later = world.fight(1, 4, date(2021, 12, 1), winner=1, stats={1: ROW, 4: ROW})
    world.build()
    before = world.features()
    assert before[(later, 1)]["prior_ufc_fights"] == 2

    world.move(later, date(2021, 6, 1))  # onto the target fight's date
    world.build()
    after = world.features()

    for fighter_id in (1, 3):
        assert after[(target, fighter_id)] == before[(target, fighter_id)]
    # The moved fight sits on the same date, so it sees `first` but not the target (and vice versa).
    assert after[(later, 1)]["prior_ufc_fights"] == 1
    assert after[(target, 1)]["prior_ufc_fights"] == 1
