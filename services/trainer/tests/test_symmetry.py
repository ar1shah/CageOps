"""P(A wins) + P(B wins) == 1, exactly, and swapping the fighters swaps the answer.

This is the property the swap-and-average prediction exists to guarantee: a model trained on
random order is only symmetric on average, and a probability pair that sums to 0.98 is a bug a
user would see."""

import numpy as np
import pytest

from cageops_common.inference import features as inf
from cageops_common.inference.model import MethodModel, WinLossModel
from cageops_trainer import features as f
from cageops_trainer.model import fit, params_for

N = len(inf.NUMERIC_FEATURES)


@pytest.fixture(scope="module")
def models():
    rng = np.random.default_rng(0)
    n = 600
    skill_a, skill_b = rng.normal(size=n), rng.normal(size=n)
    a = rng.normal(size=(n, N))
    b = rng.normal(size=(n, N))
    a[:, 0], b[:, 0] = skill_a, skill_b
    stances = ["orthodox", "southpaw", "switch"]
    sa = list(rng.choice(stances, n))
    sb = list(rng.choice(stances, n))
    wc = list(rng.choice(["Lightweight", "Welterweight"], n))

    swap = f.random_swap(n, 1)
    won_first = (skill_a + rng.normal(0, 0.5, n) > skill_b).astype(int)
    a2, b2, sa2, sb2 = f.swap_sides(swap, a, b, sa, sb)
    y = (won_first ^ swap).astype(int)

    ordered = f.fit_vocabulary(sa, sb, wc, ordered=True)
    x = inf.win_loss_matrix(a2, b2, sa2, sb2, wc, ordered)
    win = WinLossModel(
        fit(
            x[:450], y[:450], x[450:], y[450:],
            feature_names=inf.WIN_FEATURE_NAMES, params=params_for("binary", 0),
        ),
        ordered,
    )  # fmt: skip

    unordered = f.fit_vocabulary(sa, sb, wc, ordered=False)
    method_y = rng.integers(0, 3, n)
    xm = inf.method_matrix(a, b, sa, sb, wc, unordered)
    method = MethodModel(
        fit(
            xm[:450], method_y[:450], xm[450:], method_y[450:],
            feature_names=inf.METHOD_FEATURE_NAMES,
            params=params_for("multiclass", 0, num_class=3),
        ),
        unordered,
    )  # fmt: skip
    return win, method, (a, b, sa, sb, wc)


def test_the_two_win_probabilities_sum_to_one(models):
    win, _, (a, b, sa, sb, wc) = models

    p_a = win.predict(a, b, sa, sb, wc)
    p_b = win.predict(b, a, sb, sa, wc)

    assert p_a + p_b == pytest.approx(np.ones(len(a)), abs=1e-12)
    assert np.all((p_a > 0) & (p_a < 1))


def test_asking_with_the_fighters_swapped_returns_the_mirror_image(models):
    win, _, (a, b, sa, sb, wc) = models

    p_a = win.predict(a, b, sa, sb, wc)
    p_b = win.predict(b, a, sb, sa, wc)

    assert p_b == pytest.approx(1 - p_a, abs=1e-12)


def test_averaging_matters_the_raw_model_alone_is_not_symmetric(models):
    """If this ever became exactly symmetric the averaging would be dead code; keep it honest."""
    win, _, (a, b, sa, sb, wc) = models

    raw_ab = win._raw(a, b, sa, sb, wc)
    raw_ba = win._raw(b, a, sb, sa, wc)

    assert not np.allclose(raw_ab + raw_ba, 1.0, atol=1e-9)


def test_method_probabilities_sum_to_one_and_do_not_depend_on_the_order(models):
    _, method, (a, b, sa, sb, wc) = models

    forward = method.predict(a, b, sa, sb, wc)
    backward = method.predict(b, a, sb, sa, wc)

    assert forward.sum(axis=1) == pytest.approx(np.ones(len(a)), abs=1e-9)
    assert np.array_equal(forward, backward)
