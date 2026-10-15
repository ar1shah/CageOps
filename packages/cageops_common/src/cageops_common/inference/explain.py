"""Which features moved a win probability, in fighter A's favor or against.

The headline probability is `WinLossModel.predict`: the model is asked in both fighter orders and
the answers are averaged, `p_a = (sigmoid(z_ab) + 1 - sigmoid(z_ba)) / 2`, where `z` is the raw
log-odds margin. Contributions have to be averaged the same way or they would explain a different
number.

LightGBM's `pred_contrib=True` returns, per row, one TreeSHAP value per feature plus a bias, in
raw log-odds, and they sum to that row's `z`. Averaging PROBABILITIES is not linear in them, but
averaging LOG-ODDS is, and it is exact: `1 - sigmoid(z_ba) = sigmoid(-z_ba)`, so from A's side

    z_a = (z_ab - z_ba) / 2            per feature: phi_f = (c_ab[f] - c_ba[f]) / 2
    baseline    = (bias_ab - bias_ba) / 2        and   sum(phi) + baseline = z_a   exactly.

Consequences, all tested: swapping A and B negates every phi exactly; `p_a > 0.5` iff `z_a > 0`
(since sigmoid is monotone), so the direction of the contributions always agrees with the
headline; and `sigmoid(z_a)` is close to but not equal to `p_a` (the mean of two probabilities is
not the probability of the mean logit), so contributions are log-odds and are never presented as
adding up to the displayed percentage.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from cageops_common.inference.features import NUMERIC_FEATURES, WIN_FEATURE_NAMES, win_loss_matrix
from cageops_common.inference.model import WinLossModel


@dataclass(frozen=True)
class WinExplanation:
    p_a: np.ndarray  # (n,) WinLossModel.predict
    z_a: np.ndarray  # (n,) both-orders-averaged log-odds for A
    baseline: np.ndarray  # (n,)
    contributions: (
        np.ndarray
    )  # (n, len(WIN_FEATURE_NAMES)), A's perspective, WIN_FEATURE_NAMES order


def explain_win(
    model: WinLossModel,
    a_values: np.ndarray,
    b_values: np.ndarray,
    a_stance: Sequence[str],
    b_stance: Sequence[str],
    weight_class: Sequence[str | None],
) -> WinExplanation:
    booster = model.booster
    x_ab = win_loss_matrix(a_values, b_values, a_stance, b_stance, weight_class, model.vocab)
    x_ba = win_loss_matrix(b_values, a_values, b_stance, a_stance, weight_class, model.vocab)
    c_ab = booster.predict(x_ab, pred_contrib=True, num_iteration=booster.best_iteration)
    c_ba = booster.predict(x_ba, pred_contrib=True, num_iteration=booster.best_iteration)
    n_features = len(WIN_FEATURE_NAMES)
    z_ab, z_ba = c_ab.sum(axis=1), c_ba.sum(axis=1)
    return WinExplanation(
        p_a=model.predict(a_values, b_values, a_stance, b_stance, weight_class),
        z_a=(z_ab - z_ba) / 2.0,
        baseline=(c_ab[:, n_features] - c_ba[:, n_features]) / 2.0,
        contributions=(c_ab[:, :n_features] - c_ba[:, :n_features]) / 2.0,
    )


@dataclass(frozen=True)
class Contribution:
    feature: str  # base name: "sig_str_landed_pm_career", "stance_matchup", "weight_class"
    kind: str  # "numeric" | "categorical"
    value_a: float | str | None
    value_b: float | str | None
    phi: float  # log-odds, A's perspective


def top_contributions(
    phi: np.ndarray,
    a_values: np.ndarray,
    b_values: np.ndarray,
    a_stance: str,
    b_stance: str,
    weight_class: str | None,
    n: int,
) -> list[Contribution]:
    """The `n` features with the largest |phi| for ONE fight (1-D arrays), nonzero only.

    Numeric model inputs are differences (`diff_x = a_x - b_x`); what is shown is each fighter's
    own value of `x`, which is what a person can read. Ties break by feature order so the same
    fight always lists the same features in the same order."""
    order = sorted(range(len(phi)), key=lambda i: (-abs(phi[i]), i))
    shown: list[Contribution] = []
    for i in order:
        if len(shown) == n or phi[i] == 0.0:
            break
        name = WIN_FEATURE_NAMES[i]
        if name == "stance_matchup":
            item = Contribution("stance_matchup", "categorical", a_stance, b_stance, float(phi[i]))
        elif name == "weight_class":
            item = Contribution(
                "weight_class", "categorical", weight_class, weight_class, float(phi[i])
            )
        else:
            base = name.removeprefix("diff_")
            j = NUMERIC_FEATURES.index(base)
            item = Contribution(
                base, "numeric", _plain(a_values[j]), _plain(b_values[j]), float(phi[i])
            )
        shown.append(item)
    return shown


def _plain(value: float) -> float | None:
    """NaN is "missing", which JSON cannot carry; it becomes null."""
    return None if np.isnan(value) else float(value)
