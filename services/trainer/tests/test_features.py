"""Every fight_features column is decided on, the swap is a pure exchange, and the method inputs
cannot depend on which fighter is called A."""

import numpy as np

from cageops_common.db.models import FightFeatures
from cageops_common.inference import features as inf
from cageops_trainer import features as f

N = len(inf.NUMERIC_FEATURES)


def test_every_column_of_fight_features_is_used_or_excluded_with_a_reason():
    columns = {c.name for c in FightFeatures.__table__.columns}
    used = set(inf.NUMERIC_FEATURES) | set(inf.CATEGORICAL_COLUMNS)
    excluded = set(f.EXCLUDED_COLUMNS)

    assert used & excluded == set()
    assert columns == used | excluded, (
        f"decide on {sorted(columns - used - excluded)} in features.py "
        f"(and drop {sorted((used | excluded) - columns)})"
    )
    assert all(f.EXCLUDED_COLUMNS.values()), "every exclusion needs a reason"


def test_the_pinned_counts_and_the_named_exclusions():
    assert len(inf.NUMERIC_FEATURES) == 34
    assert len(f.EXCLUDED_COLUMNS) == 13
    assert len(inf.WIN_FEATURE_NAMES) == 36
    assert len(inf.METHOD_FEATURE_NAMES) == 70
    assert {"is_title_fight", "scheduled_rounds"} <= set(f.EXCLUDED_COLUMNS)
    assert inf.SCHEDULE_FEATURE_NAMES == ("is_title_fight", "scheduled_rounds")
    assert inf.WIN_FEATURE_NAMES[0] == "diff_sig_str_landed_pm_career"
    assert inf.WIN_FEATURE_NAMES[-2:] == ("stance_matchup", "weight_class")


def rows(n: int, seed: int = 0):
    rng = np.random.default_rng(seed)
    a, b = rng.normal(size=(n, N)), rng.normal(size=(n, N))
    stances = np.array(["orthodox", "southpaw", "switch", "other", "unknown"])
    sa, sb = list(rng.choice(stances, n)), list(rng.choice(stances, n))
    return a, b, sa, sb, ["Lightweight"] * n


def vocabularies(a, b, sa, sb, wc):
    # 300 fights over at most 25 ordered pairs: every pair clears the minimum of 30 most of the time
    return (
        f.fit_vocabulary(sa, sb, wc, ordered=True),
        f.fit_vocabulary(sa, sb, wc, ordered=False),
    )


def test_swapping_negates_every_difference_and_reverses_the_stance_matchup():
    a, b, sa, sb, wc = rows(300)
    ordered, _ = vocabularies(a, b, sa, sb, wc)
    swap = np.ones(300, dtype=bool)

    a2, b2, sa2, sb2 = f.swap_sides(swap, a, b, sa, sb)
    plain = inf.win_loss_matrix(a, b, sa, sb, wc, ordered)
    swapped = inf.win_loss_matrix(a2, b2, sa2, sb2, wc, ordered)

    assert np.array_equal(swapped[:, :N], -plain[:, :N], equal_nan=True)
    assert sa2 == sb and sb2 == sa
    # the matchup of the swapped row is the reverse string of the plain one
    for i in range(300):
        assert inf.stance_matchup(sa2[i], sb2[i]) == inf.stance_matchup(sb[i], sa[i])


def test_a_partial_swap_exchanges_exactly_the_flagged_fights():
    a, b, sa, sb, _ = rows(10)
    swap = np.array([True, False] * 5)

    a2, b2, sa2, sb2 = f.swap_sides(swap, a, b, sa, sb)

    assert np.array_equal(a2[swap], b[swap]) and np.array_equal(b2[swap], a[swap])
    assert np.array_equal(a2[~swap], a[~swap]) and np.array_equal(b2[~swap], b[~swap])
    assert [sa2[i] for i in range(10)] == [sb[i] if swap[i] else sa[i] for i in range(10)]


def test_the_swap_is_seeded_and_about_half():
    first, again = f.random_swap(10_000, 7), f.random_swap(10_000, 7)

    assert np.array_equal(first, again)
    assert not np.array_equal(first, f.random_swap(10_000, 8))
    assert 0.47 < first.mean() < 0.53


def test_rare_matchups_collapse_and_the_vocabulary_is_built_from_the_fights_it_is_given():
    sa = ["orthodox"] * 40 + ["switch"] * 5
    sb = ["southpaw"] * 40 + ["unknown"] * 5
    wc = ["Lightweight"] * 45

    vocab = f.fit_vocabulary(sa, sb, wc, ordered=True)

    assert "orthodox_vs_southpaw" in vocab.stance_matchups
    assert "southpaw_vs_orthodox" in vocab.stance_matchups  # the swap shows both orientations
    assert "switch_vs_unknown" not in vocab.stance_matchups  # 5 < 30
    other = float(vocab.stance_matchups.index(inf.OTHER_MATCHUP))
    assert vocab.matchup_code("switch", "unknown") == other
    assert vocab.matchup_code("other", "other") == other  # never seen at all


def test_the_unordered_vocabulary_treats_a_pair_and_its_reverse_as_one():
    sa = ["orthodox"] * 20 + ["southpaw"] * 20
    sb = ["southpaw"] * 20 + ["orthodox"] * 20

    vocab = f.fit_vocabulary(sa, sb, ["Lightweight"] * 40, ordered=False)

    assert vocab.matchup_code("orthodox", "southpaw") == vocab.matchup_code("southpaw", "orthodox")
    assert vocab.stance_matchups.count("orthodox_vs_southpaw") == 1


def test_an_unseen_or_missing_weight_class_is_missing_not_a_new_category():
    vocab = f.fit_vocabulary(["orthodox"], ["orthodox"], ["Lightweight"], ordered=True)

    assert vocab.weight_class_code("Lightweight") == 0.0
    assert np.isnan(vocab.weight_class_code("Catch Weight"))
    assert np.isnan(vocab.weight_class_code(None))


def test_a_vocabulary_survives_a_round_trip_through_json():
    vocab = f.fit_vocabulary(
        ["orthodox"] * 40, ["southpaw"] * 40, ["Lightweight"] * 40, ordered=True
    )

    assert inf.Vocabulary.from_dict(vocab.as_dict()) == vocab
