"""The inference half of the feature code: pure functions, no database, no trainer.

(Which columns are excluded, vocabulary fitting and the training swap are tested in the trainer.)
"""

import numpy as np
import pytest

from cageops_common.inference import features as f

N = len(f.NUMERIC_FEATURES)


def test_the_model_input_names_are_pinned():
    assert len(f.NUMERIC_FEATURES) == 34
    assert len(f.WIN_FEATURE_NAMES) == 36
    assert len(f.METHOD_FEATURE_NAMES) == 70
    assert f.SCHEDULE_FEATURE_NAMES == ("is_title_fight", "scheduled_rounds")
    assert f.WIN_FEATURE_NAMES[0] == "diff_sig_str_landed_pm_career"
    assert f.WIN_FEATURE_NAMES[-2:] == ("stance_matchup", "weight_class")
    assert len(set(f.NUMERIC_FEATURES)) == 34


def test_no_model_input_is_an_odds_or_ranking_or_identity_column():
    names = [*f.WIN_FEATURE_NAMES, *f.METHOD_FEATURE_NAMES, *f.NUMERIC_FEATURES]
    for name in names:
        for banned in ("odds", "rank", "fighter_id", "fight_id", "winner", "method"):
            assert banned not in name


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Orthodox", "orthodox"),
        (" Southpaw ", "southpaw"),
        ("Switch", "switch"),
        ("Open Stance", "other"),
        ("Sideways", "other"),
        (None, "unknown"),
        ("", "unknown"),
    ],
)
def test_stance_is_normalized(raw, expected):
    assert f.normalize_stance(raw) == expected


def vocab(ordered: bool) -> f.Vocabulary:
    stances = ["orthodox", "southpaw", "switch", "other", "unknown"]
    pairs = [f.stance_matchup(a, b) for a in stances for b in stances]
    if not ordered:
        pairs = sorted({f.sorted_stance_matchup(a, b) for a in stances for b in stances})
    return f.Vocabulary(
        ordered=ordered,
        stance_matchups=(*pairs, f.OTHER_MATCHUP),
        weight_classes=("Lightweight", "Welterweight"),
    )


def rows(n: int, seed: int = 0):
    rng = np.random.default_rng(seed)
    a, b = rng.normal(size=(n, N)), rng.normal(size=(n, N))
    stances = np.array(["orthodox", "southpaw", "switch", "other", "unknown"])
    sa, sb = list(rng.choice(stances, n)), list(rng.choice(stances, n))
    return a, b, sa, sb, ["Lightweight"] * n


def test_method_inputs_are_identical_whichever_fighter_is_called_a():
    a, b, sa, sb, wc = rows(300, seed=3)

    forward = f.method_matrix(a, b, sa, sb, wc, vocab(ordered=False))
    backward = f.method_matrix(b, a, sb, sa, wc, vocab(ordered=False))

    assert np.array_equal(forward, backward, equal_nan=True)


def test_win_loss_inputs_negate_when_the_fighters_are_exchanged():
    a, b, sa, sb, wc = rows(50, seed=4)
    v = vocab(ordered=True)

    ab = f.win_loss_matrix(a, b, sa, sb, wc, v)
    ba = f.win_loss_matrix(b, a, sb, sa, wc, v)

    assert np.array_equal(ba[:, :N], -ab[:, :N], equal_nan=True)


def test_a_missing_value_on_either_side_is_missing_in_the_difference_and_the_method_inputs():
    a = np.zeros((1, N))
    b = np.zeros((1, N))
    a[0, 2] = np.nan

    win = f.win_loss_matrix(a, b, ["orthodox"], ["orthodox"], ["Lightweight"], vocab(True))
    method = f.method_matrix(a, b, ["orthodox"], ["orthodox"], ["Lightweight"], vocab(False))

    assert np.isnan(win[0, 2])
    assert np.isnan(method[0, 2]) and np.isnan(method[0, N + 2])
    assert not np.isnan(win[0, 3])


def test_an_unseen_or_missing_weight_class_is_missing_not_a_new_category():
    v = vocab(True)

    assert v.weight_class_code("Lightweight") == 0.0
    assert np.isnan(v.weight_class_code("Catch Weight"))
    assert np.isnan(v.weight_class_code(None))


def test_an_unseen_stance_matchup_becomes_other_matchup():
    v = f.Vocabulary(True, ("orthodox_vs_southpaw", f.OTHER_MATCHUP), ())

    assert v.matchup_code("switch", "unknown") == 1.0


def test_a_vocabulary_survives_a_round_trip_through_json():
    v = vocab(True)

    assert f.Vocabulary.from_dict(v.as_dict()) == v
