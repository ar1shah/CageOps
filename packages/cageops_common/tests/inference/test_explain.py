"""The contribution maths (inference/explain.py): additive, mirrored, and consistent with the
headline. Tiny boosters trained here on random data; no database."""

import lightgbm as lgb
import numpy as np
import pytest

from cageops_common.inference import features as f
from cageops_common.inference.explain import explain_win, top_contributions
from cageops_common.inference.model import WinLossModel

N = len(f.NUMERIC_FEATURES)
STANCES = ["orthodox", "southpaw", "switch", "other", "unknown"]


def vocab():
    pairs = sorted(f.stance_matchup(a, b) for a in STANCES for b in STANCES)
    return f.Vocabulary(True, (*pairs, f.OTHER_MATCHUP), ("Lightweight", "Welterweight"))


@pytest.fixture(scope="module")
def model():
    rng = np.random.default_rng(3)
    n = 800
    a, b = rng.normal(size=(n, N)), rng.normal(size=(n, N))
    sa, sb = list(rng.choice(STANCES, n)), list(rng.choice(STANCES, n))
    wc = list(rng.choice(["Lightweight", "Welterweight"], n))
    a[rng.random(a.shape) < 0.1] = np.nan  # missing values, as in the real features
    x = f.win_loss_matrix(a, b, sa, sb, wc, vocab())
    # signal in two numeric features and in the weight class, so contributions are not all ~0
    y = (a[:, 0] - b[:, 0]).clip(-3, 3) + np.nan_to_num(x[:, 2]) * 0.3 + rng.normal(0, 1, n) > 0
    booster = lgb.train(
        {"objective": "binary", "verbosity": -1, "num_leaves": 8, "seed": 1},
        lgb.Dataset(
            x, label=y.astype(int), feature_name=list(f.WIN_FEATURE_NAMES),
            categorical_feature=list(f.CATEGORICAL_FEATURE_NAMES),
        ),
        num_boost_round=30,
    )  # fmt: skip
    return WinLossModel(booster, vocab())


def sample(seed=0, n=40):
    rng = np.random.default_rng(seed)
    a, b = rng.normal(size=(n, N)), rng.normal(size=(n, N))
    a[rng.random(a.shape) < 0.1] = np.nan
    sa, sb = list(rng.choice(STANCES, n)), list(rng.choice(STANCES, n))
    wc = list(rng.choice(["Lightweight", "Welterweight", None], n))
    return a, b, sa, sb, wc


def test_contributions_plus_baseline_equal_the_averaged_log_odds_exactly(model):
    a, b, sa, sb, wc = sample()

    e = explain_win(model, a, b, sa, sb, wc)

    raw_ab = model.booster.predict(f.win_loss_matrix(a, b, sa, sb, wc, model.vocab), raw_score=True)
    raw_ba = model.booster.predict(f.win_loss_matrix(b, a, sb, sa, wc, model.vocab), raw_score=True)
    assert e.z_a == pytest.approx((raw_ab - raw_ba) / 2, abs=1e-9)
    assert e.contributions.sum(axis=1) + e.baseline == pytest.approx(e.z_a, abs=1e-9)
    assert e.contributions.shape == (40, len(f.WIN_FEATURE_NAMES))


def test_asking_with_the_fighters_swapped_negates_every_contribution_exactly(model):
    a, b, sa, sb, wc = sample(1)

    forward = explain_win(model, a, b, sa, sb, wc)
    backward = explain_win(model, b, a, sb, sa, wc)

    assert np.array_equal(backward.contributions, -forward.contributions)
    assert np.array_equal(backward.z_a, -forward.z_a)
    assert np.array_equal(backward.baseline, -forward.baseline)
    assert backward.p_a == pytest.approx(1 - forward.p_a, abs=1e-12)


def test_the_direction_always_agrees_with_the_headline_probability(model):
    a, b, sa, sb, wc = sample(2, n=300)

    e = explain_win(model, a, b, sa, sb, wc)

    decisive = np.abs(e.p_a - 0.5) > 1e-9
    assert np.array_equal((e.p_a > 0.5)[decisive], (e.z_a > 0)[decisive])
    # The same direction but NOT the same number: the mean of two probabilities is not the
    # probability of the mean logit. On this deliberately confident random model they differ by
    # a few points, which is why contributions are labelled log-odds and never claimed to add up
    # to the displayed percentage.
    sigmoid = 1 / (1 + np.exp(-e.z_a))
    gap = np.abs(sigmoid - e.p_a)
    assert gap.max() < 0.15
    assert gap.max() > 0.001


def test_the_headline_is_the_models_own_predict(model):
    a, b, sa, sb, wc = sample(3)

    assert np.array_equal(explain_win(model, a, b, sa, sb, wc).p_a, model.predict(a, b, sa, sb, wc))


def test_top_contributions_are_the_largest_by_magnitude_in_a_stable_order(model):
    a, b, sa, sb, wc = sample(4, n=1)
    e = explain_win(model, a, b, sa, sb, wc)

    shown = top_contributions(e.contributions[0], a[0], b[0], sa[0], sb[0], wc[0], 5)

    assert 0 < len(shown) <= 5
    magnitudes = [abs(c.phi) for c in shown]
    assert magnitudes == sorted(magnitudes, reverse=True)
    assert magnitudes[0] == np.abs(e.contributions[0]).max()
    again = top_contributions(e.contributions[0], a[0], b[0], sa[0], sb[0], wc[0], 5)
    assert [c.feature for c in again] == [c.feature for c in shown]


def test_a_numeric_input_is_shown_as_each_fighters_own_value_not_the_difference(model):
    a, b, sa, sb, wc = sample(5, n=1)
    e = explain_win(model, a, b, sa, sb, wc)

    everything = top_contributions(
        e.contributions[0], a[0], b[0], sa[0], sb[0], wc[0], len(f.WIN_FEATURE_NAMES)
    )

    for c in everything:
        if c.kind == "numeric":
            j = f.NUMERIC_FEATURES.index(c.feature)
            assert c.value_a == (None if np.isnan(a[0, j]) else a[0, j])
            assert c.value_b == (None if np.isnan(b[0, j]) else b[0, j])
        else:
            assert c.feature in ("stance_matchup", "weight_class")
    assert "diff_" not in "".join(c.feature for c in everything)


def test_zero_contributions_are_not_listed():
    phi = np.zeros(len(f.WIN_FEATURE_NAMES))
    phi[0] = 0.5
    values = np.zeros(N)

    shown = top_contributions(phi, values, values, "orthodox", "southpaw", "Lightweight", 5)

    assert [c.feature for c in shown] == [f.NUMERIC_FEATURES[0]]
