"""Fitting the two LightGBM models (D-031, D-032).

How they predict, and how they are saved and loaded, is shared with the API and lives in
`cageops_common.inference.model`.

Determinism: single-threaded, `deterministic=True`, row-wise histograms and every seed set, so the
same data and seed give the same model on the same library versions.
"""

from __future__ import annotations

from collections.abc import Sequence

import lightgbm as lgb
import numpy as np

from cageops_common.inference.features import CATEGORICAL_FEATURE_NAMES

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
