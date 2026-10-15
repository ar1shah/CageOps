"""Turning two fighters' `fight_features` rows into a model's input (D-031, D-032).

Every column of `fight_features` is accounted for below: used (numeric, or categorical) or
excluded with a reason. A test fails if a column is added to the table and not decided on here.

Win/loss sees the DIFFERENCE between the two fighters. Method-of-victory sees the MEAN and the
absolute GAP, because "how does this fight end" does not depend on which fighter is called A.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

STATS = (
    "sig_str_landed_pm",
    "sig_str_absorbed_pm",
    "sig_str_acc",
    "sig_str_def",
    "td_landed_per15",
    "td_def",
    "sub_att_per15",
    "kd_per15",
)
WINDOWS = ("career", "last3", "last5")

NUMERIC_FEATURES: tuple[str, ...] = (
    *(f"{stat}_{window}" for stat in STATS for window in WINDOWS),  # 24 skill rates
    *(f"n_fights_{window}" for window in WINDOWS),  # 3 experience counts
    "prior_ufc_fights",
    "win_streak",
    "finish_rate",
    "days_since_last_fight",
    "age_days",
    "height_cm",
    "reach_cm",
)
CATEGORICAL_COLUMNS = ("stance", "weight_class")
# Left out of the shipped models: NULL for upcoming bouts and for the Wikipedia period (D-027,
# D-029). Only the ablation (D-032) ever trains with them.
SCHEDULE_COLUMNS = ("is_title_fight", "scheduled_rounds")

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

STANCES = ("orthodox", "southpaw", "switch")
UNKNOWN_STANCE = "unknown"
OTHER_STANCE = "other"
OTHER_MATCHUP = "other_matchup"
MIN_MATCHUP_COUNT = 30  # an ordered stance pair seen fewer times in train collapses

WIN_FEATURE_NAMES = (
    *(f"diff_{name}" for name in NUMERIC_FEATURES),
    "stance_matchup",
    "weight_class",
)
METHOD_FEATURE_NAMES = (
    *(f"mean_{name}" for name in NUMERIC_FEATURES),
    *(f"gap_{name}" for name in NUMERIC_FEATURES),
    "stance_matchup",
    "weight_class",
)
CATEGORICAL_FEATURE_NAMES = ("stance_matchup", "weight_class")
SCHEDULE_FEATURE_NAMES = SCHEDULE_COLUMNS


def normalize_stance(raw: str | None) -> str:
    """Orthodox | southpaw | switch; Open Stance and Sideways become `other`; NULL `unknown`."""
    if raw is None or not raw.strip():
        return UNKNOWN_STANCE
    value = raw.strip().lower()
    return value if value in STANCES else OTHER_STANCE


def stance_matchup(first: str, second: str) -> str:
    return f"{first}_vs_{second}"


def sorted_stance_matchup(first: str, second: str) -> str:
    return stance_matchup(*sorted((first, second)))


@dataclass(frozen=True)
class Vocabulary:
    """Category codes, fit on the TRAIN split only and stored with the model.

    A stance matchup not in the list becomes `other_matchup`. A weight class not in the list
    (or NULL) is missing, which LightGBM handles natively."""

    ordered: bool  # True: "orthodox_vs_southpaw" differs from its reverse (win/loss)
    stance_matchups: tuple[str, ...]
    weight_classes: tuple[str, ...]

    def matchup_code(self, first: str, second: str) -> float:
        key = (
            stance_matchup(first, second) if self.ordered else sorted_stance_matchup(first, second)
        )
        if key not in self.stance_matchups:
            key = OTHER_MATCHUP
        return float(self.stance_matchups.index(key))

    def weight_class_code(self, weight_class: str | None) -> float:
        if weight_class is None or weight_class not in self.weight_classes:
            return float("nan")
        return float(self.weight_classes.index(weight_class))

    def as_dict(self) -> dict[str, object]:
        return {
            "ordered": self.ordered,
            "stance_matchups": list(self.stance_matchups),
            "weight_classes": list(self.weight_classes),
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Vocabulary:
        return cls(
            ordered=bool(data["ordered"]),
            stance_matchups=tuple(data["stance_matchups"]),  # type: ignore[arg-type]
            weight_classes=tuple(data["weight_classes"]),  # type: ignore[arg-type]
        )


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


def _categorical_columns(
    vocab: Vocabulary,
    a_stance: Sequence[str],
    b_stance: Sequence[str],
    weight_class: Sequence[str | None],
) -> np.ndarray:
    matchup = [vocab.matchup_code(a, b) for a, b in zip(a_stance, b_stance, strict=True)]
    weight = [vocab.weight_class_code(wc) for wc in weight_class]
    return np.column_stack([matchup, weight]).astype(float)


def win_loss_matrix(
    a_values: np.ndarray,
    b_values: np.ndarray,
    a_stance: Sequence[str],
    b_stance: Sequence[str],
    weight_class: Sequence[str | None],
    vocab: Vocabulary,
) -> np.ndarray:
    """(first - second) for every numeric feature, plus the stance matchup and weight class."""
    return np.hstack(
        [a_values - b_values, _categorical_columns(vocab, a_stance, b_stance, weight_class)]
    )


def method_matrix(
    a_values: np.ndarray,
    b_values: np.ndarray,
    a_stance: Sequence[str],
    b_stance: Sequence[str],
    weight_class: Sequence[str | None],
    vocab: Vocabulary,
    schedule: np.ndarray | None = None,
) -> np.ndarray:
    """Mean and absolute gap of every numeric feature: identical for (A, B) and (B, A).

    `schedule` (n, 2) holds is_title_fight and scheduled_rounds, and is passed only by the
    ablation; the shipped model never sees it."""
    parts = [
        (a_values + b_values) / 2.0,
        np.abs(a_values - b_values),
        _categorical_columns(vocab, a_stance, b_stance, weight_class),
    ]
    if schedule is not None:
        parts.append(schedule)
    return np.hstack(parts)
