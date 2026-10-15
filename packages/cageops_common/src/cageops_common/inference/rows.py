"""The one place a database row becomes a model input.

Training reads `fight_features` rows and serving reads `fighter_features_current` rows; both go
through `rows_to_arrays`, so the column order, the NULL-to-NaN rule and the stance normalization
are written once.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

import numpy as np

from cageops_common.inference.asof import date_relative_features
from cageops_common.inference.features import NUMERIC_FEATURES, normalize_stance


def number(value: Any) -> float:
    """NULL is NaN (LightGBM's "missing"), never 0."""
    return float("nan") if value is None else float(value)


def row_to_inputs(row: Mapping[str, Any]) -> tuple[np.ndarray, str]:
    """One fighter's (34 values in NUMERIC_FEATURES order, normalized stance)."""
    values = np.array([number(row[name]) for name in NUMERIC_FEATURES], dtype=float)
    return values, normalize_stance(row["stance"])


def rows_to_arrays(rows: Sequence[Mapping[str, Any]]) -> tuple[np.ndarray, list[str]]:
    """(n, 34) values and n normalized stances, for n fighters."""
    values = np.full((len(rows), len(NUMERIC_FEATURES)), np.nan)
    stances: list[str] = []
    for i, row in enumerate(rows):
        values[i], stance = row_to_inputs(row)
        stances.append(stance)
    return values, stances


def with_date_relative(row: Mapping[str, Any], as_of: date) -> dict[str, Any]:
    """A `fighter_features_current` row with `age_days` and `days_since_last_fight` filled in for
    `as_of`, from the row's `dob` and `last_fight_date`. The same function the feature builder
    calls (`asof.date_relative_features`), so a stored row is right for any date it is asked
    about (D-037)."""
    return {**row, **date_relative_features(as_of, row.get("dob"), row.get("last_fight_date"))}
