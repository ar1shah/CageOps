"""The two baselines a model has to be read against (rule 4).

1. The betting favorite, from mdabbert closing odds, on the fights that have them.
2. A simple experience/ranking heuristic that needs no model.

Odds are baselines only, never features (D-006, D-008).
"""

from __future__ import annotations

import numpy as np

from cageops_common.inference.features import NUMERIC_FEATURES

PRIOR_UFC_FIGHTS = NUMERIC_FEATURES.index("prior_ufc_fights")


def favorite_probability(odds_a: np.ndarray, odds_b: np.ndarray) -> np.ndarray:
    """P(fighter A wins) from decimal odds, with the bookmaker's margin removed.

    1/odds is each side's implied probability; the two sum to more than 1 (the margin), so they
    are rescaled to sum to 1. Equal odds give exactly 0.5, which the scorer counts as a tie."""
    implied_a, implied_b = 1.0 / odds_a, 1.0 / odds_b
    return implied_a / (implied_a + implied_b)


def heuristic_sign(
    rank_a: np.ndarray, rank_b: np.ndarray, prior_a: np.ndarray, prior_b: np.ndarray
) -> np.ndarray:
    """+1 if the rule picks A, -1 if it picks B, 0 if it cannot choose.

    If both fighters are ranked in the division, the better rank (lower number; 0 is the
    champion) wins. Otherwise the fighter with more UFC fights wins. Ranks come only from the
    point-in-time `fight_pre_rankings` view, so nothing here looks past the fight date."""
    both_ranked = ~np.isnan(rank_a) & ~np.isnan(rank_b)
    by_rank = np.sign(rank_b - rank_a)
    by_experience = np.sign(prior_a - prior_b)
    return np.where(both_ranked, by_rank, by_experience)


def fit_heuristic(sign: np.ndarray, a_won: np.ndarray) -> float:
    """How often the rule's pick wins, over fights where it picked, on the training split only.

    The rule alone gives a pick, not a probability; this one number turns it into one so that
    log loss and Brier can be computed for it."""
    picked = sign != 0
    return float(((sign[picked] == 1) == a_won[picked]).mean())


def heuristic_probability(sign: np.ndarray, pick_win_rate: float) -> np.ndarray:
    return 0.5 + sign * (pick_win_rate - 0.5)
