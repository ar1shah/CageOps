"""The baselines: the favorite is de-vigged odds, the heuristic is a rule plus one number."""

import numpy as np
import pytest

from cageops_trainer import baselines


def test_the_favorite_probability_removes_the_bookmakers_margin():
    # 1/1.5 + 1/2.6 = 0.6667 + 0.3846 = 1.0513: the 5% is the margin.
    p = baselines.favorite_probability(np.array([1.5]), np.array([2.6]))

    assert p[0] == pytest.approx(0.6667 / 1.0513, rel=1e-3)
    assert p[0] > 0.5


def test_a_probability_and_its_mirror_sum_to_one():
    a, b = np.array([1.4, 3.1, 2.0]), np.array([3.0, 1.35, 2.0])

    forward = baselines.favorite_probability(a, b)
    mirrored = baselines.favorite_probability(b, a)

    assert forward + mirrored == pytest.approx(np.ones(3))


def test_equal_prices_give_exactly_one_half_so_they_score_as_a_tie():
    assert baselines.favorite_probability(np.array([1.9]), np.array([1.9]))[0] == 0.5


def test_the_heuristic_prefers_the_better_rank_when_both_are_ranked():
    sign = baselines.heuristic_sign(
        rank_a=np.array([3.0, 0.0, 7.0]),
        rank_b=np.array([9.0, 4.0, 7.0]),
        prior_a=np.array([1.0, 1.0, 20.0]),
        prior_b=np.array([30.0, 30.0, 5.0]),
    )

    # A is ranked higher in the first two (despite far fewer fights); equal ranks pick nobody.
    assert list(sign) == [1.0, 1.0, 0.0]


def test_the_heuristic_falls_back_to_experience_when_either_fighter_is_unranked():
    sign = baselines.heuristic_sign(
        rank_a=np.array([np.nan, 3.0, np.nan]),
        rank_b=np.array([5.0, np.nan, np.nan]),
        prior_a=np.array([4.0, 2.0, 7.0]),
        prior_b=np.array([9.0, 1.0, 7.0]),
    )

    assert list(sign) == [-1.0, 1.0, 0.0]


def test_the_heuristic_is_turned_into_a_probability_with_one_number_fit_on_train():
    sign = np.array([1, 1, 1, 1, -1, -1, -1, -1, 0, 0])
    a_won = np.array([True, True, True, False, False, False, False, True, True, False])

    rate = baselines.fit_heuristic(sign, a_won)

    assert rate == pytest.approx(
        6 / 8
    )  # the pick won in 3 of the first four and 3 of the next four
    p = baselines.heuristic_probability(np.array([1, -1, 0]), rate)
    assert list(p) == pytest.approx([0.75, 0.25, 0.5])
