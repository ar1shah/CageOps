"""The scores, against numbers worked out by hand."""

import math

import numpy as np
import pytest

from cageops_trainer import metrics


def test_log_loss_and_brier_match_a_hand_calculation():
    y = np.array([1, 0])
    p = np.array([0.8, 0.3])

    assert metrics.log_loss(y, p) == pytest.approx(-(math.log(0.8) + math.log(0.7)) / 2)
    assert metrics.brier(y, p) == pytest.approx((0.2**2 + 0.3**2) / 2)


def test_a_coin_flip_scores_ln2_and_a_quarter():
    y = np.array([1, 0, 1, 1])
    p = np.full(4, 0.5)

    assert metrics.log_loss(y, p) == pytest.approx(math.log(2))
    assert metrics.brier(y, p) == pytest.approx(0.25)


def test_log_loss_survives_a_certain_prediction_that_is_wrong():
    assert math.isfinite(metrics.log_loss(np.array([1]), np.array([0.0])))


def test_a_prediction_of_exactly_one_half_is_a_tie_not_a_coin_flip_result():
    accuracy, scored, ties = metrics.accuracy(
        np.array([1, 0, 1, 0]), np.array([0.7, 0.5, 0.2, 0.1])
    )

    assert (scored, ties) == (3, 1)
    assert accuracy == pytest.approx(2 / 3)  # 0.7 right, 0.2 wrong, 0.1 right


def test_accuracy_with_only_ties_is_none():
    assert metrics.accuracy(np.array([1, 0]), np.array([0.5, 0.5])) == (None, 0, 2)


def test_calibration_bins_are_equal_count_and_compare_predicted_to_observed():
    y = np.array([0, 0, 1, 1, 0, 1, 1, 1])
    p = np.array([0.1, 0.2, 0.3, 0.4, 0.6, 0.7, 0.8, 0.9])

    result = metrics.calibration(y, p, bins=4)

    assert [b["n"] for b in result["bins"]] == [2, 2, 2, 2]
    assert result["bins"][0]["mean_predicted"] == pytest.approx(0.15)
    assert result["bins"][0]["observed_rate"] == 0.0
    assert result["bins"][3]["observed_rate"] == 1.0
    # |predicted - observed| per bin: |0.15-0|, |0.35-1|, |0.65-0.5|, |0.85-1|
    expected = (0.15 + 0.65 + 0.15 + 0.15) / 4
    assert result["expected_calibration_error"] == pytest.approx(expected)


def test_a_perfectly_calibrated_set_has_no_calibration_error():
    p = np.repeat([0.2, 0.8], 50)
    y = np.concatenate([np.r_[np.ones(10), np.zeros(40)], np.r_[np.ones(40), np.zeros(10)]])

    assert metrics.calibration(y, p, bins=2)["expected_calibration_error"] == pytest.approx(0.0)


def test_the_bootstrap_is_seeded_and_brackets_its_own_estimate():
    rng = np.random.default_rng(0)
    a, b = rng.random(300), rng.random(300)

    first = metrics.paired_bootstrap(a, b, seed=9)
    again = metrics.paired_bootstrap(a, b, seed=9)

    assert first == again
    assert first["ci95_low"] <= first["mean_difference"] <= first["ci95_high"]
    assert first["mean_difference"] == pytest.approx(float((a - b).mean()))


def test_the_bootstrap_interval_excludes_zero_when_one_loss_is_clearly_lower():
    a, b = np.full(200, 0.5), np.full(200, 0.7)
    rng = np.random.default_rng(1)
    a, b = a + rng.normal(0, 0.01, 200), b + rng.normal(0, 0.01, 200)

    result = metrics.paired_bootstrap(a, b, seed=2)

    assert result["ci95_high"] < 0


def test_multiclass_scores():
    probs = np.array([[0.7, 0.2, 0.1], [0.1, 0.1, 0.8]])
    y = np.array([0, 2])

    assert metrics.multiclass_log_loss(y, probs) == pytest.approx(
        -(math.log(0.7) + math.log(0.8)) / 2
    )
    assert metrics.multiclass_brier(y, probs) == pytest.approx(
        ((0.3**2 + 0.2**2 + 0.1**2) + (0.1**2 + 0.1**2 + 0.2**2)) / 2
    )
    assert metrics.multiclass_accuracy(y, probs) == 1.0
