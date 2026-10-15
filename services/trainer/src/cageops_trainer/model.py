"""The two LightGBM models, and how they are asked for a prediction (D-031, D-032).

Determinism: single-threaded, `deterministic=True`, row-wise histograms and every seed set, so the
same data and seed give the same model on the same library versions.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import lightgbm as lgb
import numpy as np

from cageops_trainer.features import (
    CATEGORICAL_FEATURE_NAMES,
    METHOD_FEATURE_NAMES,
    SCHEDULE_FEATURE_NAMES,
    WIN_FEATURE_NAMES,
    Vocabulary,
    method_matrix,
    win_loss_matrix,
)

METHOD_CLASSES = ("ko_tko", "submission", "decision")

# Deliberately plain: no search. The point is the pipeline around it.
BASE_PARAMS: dict[str, object] = {
    "learning_rate": 0.03,
    "num_leaves": 15,
    "min_data_in_leaf": 50,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "lambda_l2": 5.0,
    "min_data_per_group": 50,
    "cat_smooth": 10.0,
    "verbosity": -1,
    # Reproducibility. num_threads=1 is slower than all cores and is what makes it exact.
    "deterministic": True,
    "force_row_wise": True,
    "num_threads": 1,
}
MAX_ROUNDS = 2000
PATIENCE = 100


def params_for(objective: str, seed: int, *, num_class: int | None = None) -> dict[str, object]:
    params: dict[str, object] = {
        **BASE_PARAMS,
        "objective": objective,
        "metric": "binary_logloss" if objective == "binary" else "multi_logloss",
        "seed": seed,
        "bagging_seed": seed + 1,
        "feature_fraction_seed": seed + 2,
        "data_random_seed": seed + 3,
        "extra_seed": seed + 4,
        "drop_seed": seed + 5,
    }
    if num_class is not None:
        params["num_class"] = num_class
    return params


def fit(
    x_train: np.ndarray,
    y_train: np.ndarray,
    x_val: np.ndarray,
    y_val: np.ndarray,
    *,
    feature_names: Sequence[str],
    params: dict[str, object],
) -> lgb.Booster:
    """Boost until the validation loss has not improved for PATIENCE rounds."""
    categorical = [n for n in feature_names if n in CATEGORICAL_FEATURE_NAMES]
    train_set = lgb.Dataset(
        x_train,
        label=y_train,
        feature_name=list(feature_names),
        categorical_feature=categorical,
        free_raw_data=False,
    )
    val_set = lgb.Dataset(x_val, label=y_val, reference=train_set)
    return lgb.train(
        params,
        train_set,
        num_boost_round=MAX_ROUNDS,
        valid_sets=[val_set],
        callbacks=[lgb.early_stopping(PATIENCE, verbose=False)],
    )


def booster_sha256(booster: lgb.Booster) -> str:
    return hashlib.sha256(booster.model_to_string().encode()).hexdigest()


@dataclass
class WinLossModel:
    booster: lgb.Booster
    vocab: Vocabulary

    feature_names = WIN_FEATURE_NAMES

    def _raw(self, a_values, b_values, a_stance, b_stance, weight_class) -> np.ndarray:
        """P(the FIRST fighter wins), for the given order."""
        x = win_loss_matrix(a_values, b_values, a_stance, b_stance, weight_class, self.vocab)
        return self.booster.predict(x, num_iteration=self.booster.best_iteration)

    def predict(
        self,
        a_values: np.ndarray,
        b_values: np.ndarray,
        a_stance: Sequence[str],
        b_stance: Sequence[str],
        weight_class: Sequence[str | None],
    ) -> np.ndarray:
        """P(A wins), averaged over both orderings so it cannot depend on which fighter is
        called A: p_a = (f(a, b) + 1 - f(b, a)) / 2. P(B wins) is exactly 1 - p_a, and asking
        with the fighters swapped returns the mirror image."""
        p_ab = self._raw(a_values, b_values, a_stance, b_stance, weight_class)
        p_ba = self._raw(b_values, a_values, b_stance, a_stance, weight_class)
        return (p_ab + (1.0 - p_ba)) / 2.0


@dataclass
class MethodModel:
    booster: lgb.Booster
    vocab: Vocabulary
    with_schedule: bool = False  # True only for the ablation model, which is never shipped

    @property
    def feature_names(self) -> tuple[str, ...]:
        return METHOD_FEATURE_NAMES + (SCHEDULE_FEATURE_NAMES if self.with_schedule else ())

    def predict(
        self,
        a_values: np.ndarray,
        b_values: np.ndarray,
        a_stance: Sequence[str],
        b_stance: Sequence[str],
        weight_class: Sequence[str | None],
        schedule: np.ndarray | None = None,
    ) -> np.ndarray:
        """(n, 3) probabilities in METHOD_CLASSES order. Symmetric in A and B by construction."""
        x = method_matrix(
            a_values,
            b_values,
            a_stance,
            b_stance,
            weight_class,
            self.vocab,
            schedule if self.with_schedule else None,
        )
        return self.booster.predict(x, num_iteration=self.booster.best_iteration)


def save_booster(booster: lgb.Booster, path: Path) -> str:
    """Write the model text file and return its sha256."""
    booster.save_model(str(path), num_iteration=booster.best_iteration)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_booster(path: Path) -> lgb.Booster:
    return lgb.Booster(model_file=str(path))
