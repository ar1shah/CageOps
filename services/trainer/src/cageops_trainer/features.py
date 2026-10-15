"""The training side of the feature code (D-031, D-032): which columns are excluded and why,
how a vocabulary is fit on the training split, and the training-time fighter swap.

What turns rows into model inputs (`NUMERIC_FEATURES`, the matrices, `Vocabulary`, stance
normalization) is shared with the API and lives in `cageops_common.inference.features`.
Every column of `fight_features` is accounted for: used (numeric, or categorical, there) or
excluded with a reason (here). A test fails if a column is added and not decided on.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

import numpy as np

from cageops_common.inference.features import (
    OTHER_MATCHUP,
    WINDOWS,
    Vocabulary,
    sorted_stance_matchup,
    stance_matchup,
)

MIN_MATCHUP_COUNT = 30  # an ordered stance pair seen fewer times in train collapses

EXCLUDED_COLUMNS: dict[str, str] = {
    "fight_id": "identifier: a model that sees who a fighter is memorizes fighters",
    "fighter_id": "identifier: a model that sees who a fighter is memorizes fighters",
    "built_at": "build bookkeeping",
    "load_run_id": "build bookkeeping",
    "has_unresolved_prior_fight": "data-quality flag; depends on the build's --today",
    **{
        f"n_fights_with_{kind}_{window}": "data-quality bookkeeping; it encodes era and source "
        "(pre-2000 and Wikipedia-period fights lack stats), not fighter skill"
        for kind in ("stats", "duration")
        for window in WINDOWS
    },
    "is_title_fight": "NULL for upcoming bouts and the Wikipedia period (D-032)",
    "scheduled_rounds": "NULL for upcoming bouts and the Wikipedia period (D-032)",
}


def fit_vocabulary(
    stance_a: Sequence[str],
    stance_b: Sequence[str],
    weight_class: Sequence[str | None],
    *,
    ordered: bool,
) -> Vocabulary:
    """Count matchups over the training fights, keep those seen MIN_MATCHUP_COUNT times.

    For the ordered (win/loss) vocabulary both orientations are counted, because the training
    swap shows the model every fight in both."""
    counts: Counter[str] = Counter()
    for a, b in zip(stance_a, stance_b, strict=True):
        if ordered:
            counts[stance_matchup(a, b)] += 1
            counts[stance_matchup(b, a)] += 1
        else:
            counts[sorted_stance_matchup(a, b)] += 1
    kept = sorted(key for key, n in counts.items() if n >= MIN_MATCHUP_COUNT)
    classes = sorted({wc for wc in weight_class if wc is not None})
    return Vocabulary(
        ordered=ordered,
        stance_matchups=(*kept, OTHER_MATCHUP),
        weight_classes=tuple(classes),
    )


def random_swap(n: int, seed: int) -> np.ndarray:
    """A seeded coin flip per fight: True means "put fighter B first"."""
    return np.random.default_rng(seed).random(n) < 0.5


def swap_sides(
    swap: np.ndarray,
    a_values: np.ndarray,
    b_values: np.ndarray,
    a_stance: Sequence[str],
    b_stance: Sequence[str],
) -> tuple[np.ndarray, np.ndarray, list[str], list[str]]:
    """Exchange the two fighters wherever `swap` is True. Everything that depends on order
    (negated differences, a reversed stance matchup, the label) follows from this one move."""
    mask = swap[:, None]
    first = np.where(mask, b_values, a_values)
    second = np.where(mask, a_values, b_values)
    first_stance = [b if s else a for s, a, b in zip(swap, a_stance, b_stance, strict=True)]
    second_stance = [a if s else b for s, a, b in zip(swap, a_stance, b_stance, strict=True)]
    return first, second, first_stance, second_stance
