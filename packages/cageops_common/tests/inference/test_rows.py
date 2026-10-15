"""One row of fight_features / fighter_features_current becomes one model input, in one place."""

import numpy as np

from cageops_common.inference.features import NUMERIC_FEATURES
from cageops_common.inference.rows import number, row_to_inputs, rows_to_arrays


def row(**overrides):
    base = {name: float(i) for i, name in enumerate(NUMERIC_FEATURES)}
    base["stance"] = "Southpaw"
    return {**base, **overrides}


def test_values_follow_numeric_features_order_and_the_stance_is_normalized():
    values, stance = row_to_inputs(row())

    assert values.tolist() == [float(i) for i in range(len(NUMERIC_FEATURES))]
    assert stance == "southpaw"


def test_null_is_nan_never_zero():
    assert np.isnan(number(None))
    assert number(0) == 0.0
    values, stance = row_to_inputs(row(reach_cm=None, stance=None))

    assert np.isnan(values[NUMERIC_FEATURES.index("reach_cm")])
    assert stance == "unknown"


def test_many_rows_become_one_matrix():
    values, stances = rows_to_arrays([row(), row(stance="Switch", age_days=None)])

    assert values.shape == (2, len(NUMERIC_FEATURES))
    assert stances == ["southpaw", "switch"]
    assert np.isnan(values[1, NUMERIC_FEATURES.index("age_days")])
    assert not np.isnan(values[0, NUMERIC_FEATURES.index("age_days")])
