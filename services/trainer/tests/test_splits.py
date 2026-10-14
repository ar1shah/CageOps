"""Time-based splits only (rule 4), and the proof that no training fight is from the
validation era."""

import random
from datetime import date, timedelta

import numpy as np
import pytest

from cageops_trainer.splits import (
    Boundaries,
    SplitError,
    SplitIndices,
    assert_no_leak,
    default_boundaries,
    make_splits,
    one_year_before,
)


def weekly(start: date, weeks: int, per_week: int = 3) -> list[date]:
    return [start + timedelta(weeks=w) for w in range(weeks) for _ in range(per_week)]


def test_the_default_boundaries_are_the_last_two_years_of_data():
    boundaries = default_boundaries(date(2026, 10, 3))

    assert boundaries.test_start == date(2025, 10, 4)  # the last 365 days, inclusive
    assert boundaries.val_start == date(2024, 10, 4)
    assert boundaries.train_start is None


def test_a_leap_day_has_a_one_year_before():
    assert one_year_before(date(2024, 2, 29)) == date(2023, 2, 28)
    assert one_year_before(date(2026, 10, 3)) == date(2025, 10, 3)


def test_no_training_fight_is_dated_on_or_after_the_first_validation_fight():
    dates = weekly(date(2018, 1, 6), 400)
    splits = make_splits(dates, default_boundaries(max(dates)))

    first_val = min(dates[i] for i in splits.val)
    assert all(dates[i] < first_val for i in splits.train)
    first_test = min(dates[i] for i in splits.test)
    assert all(dates[i] < first_test for i in splits.val)
    assert len(splits.train) + len(splits.val) + len(splits.test) == len(dates)


def test_a_fight_on_a_boundary_day_belongs_to_the_later_split():
    boundaries = Boundaries(val_start=date(2024, 1, 1), test_start=date(2025, 1, 1))
    dates = [date(2023, 12, 31), date(2024, 1, 1), date(2024, 12, 31), date(2025, 1, 1)]

    splits = make_splits(dates, boundaries)

    assert list(splits.train) == [0]
    assert list(splits.val) == [1, 2]
    assert list(splits.test) == [3]


@pytest.mark.parametrize("seed", range(5))
def test_the_split_is_by_date_whatever_order_the_fights_arrive_in(seed):
    dates = weekly(date(2019, 1, 5), 250)
    random.Random(seed).shuffle(dates)

    splits = make_splits(dates, default_boundaries(max(dates)))

    assert max(dates[i] for i in splits.train) < min(dates[i] for i in splits.val)
    assert max(dates[i] for i in splits.val) < min(dates[i] for i in splits.test)


def test_train_start_drops_older_fights_without_touching_val_or_test():
    dates = weekly(date(2015, 1, 3), 500)
    boundaries = default_boundaries(max(dates))
    everything = make_splits(dates, boundaries)
    recent = make_splits(
        dates, Boundaries(boundaries.val_start, boundaries.test_start, date(2020, 1, 1))
    )

    assert len(recent.train) < len(everything.train)
    assert min(dates[i] for i in recent.train) >= date(2020, 1, 1)
    assert list(recent.val) == list(everything.val)
    assert list(recent.test) == list(everything.test)


def test_an_empty_split_is_an_error_not_a_silent_pass():
    dates = weekly(date(2024, 1, 6), 10)
    with pytest.raises(SplitError, match="no fights"):
        make_splits(dates, Boundaries(date(2030, 1, 1), date(2031, 1, 1)))


def test_boundaries_must_be_ordered():
    with pytest.raises(SplitError, match="before test_start"):
        make_splits([date(2020, 1, 1)], Boundaries(date(2025, 1, 1), date(2024, 1, 1)))
    with pytest.raises(SplitError, match="train_start must be before val_start"):
        make_splits(
            [date(2020, 1, 1)], Boundaries(date(2024, 1, 1), date(2025, 1, 1), date(2024, 6, 1))
        )


def test_the_leak_check_catches_a_bad_split_even_if_the_boundaries_were_fine():
    dates = [date(2020, 1, 1), date(2021, 1, 1), date(2022, 1, 1)]
    bad = SplitIndices(train=np.array([0, 1]), val=np.array([1]), test=np.array([2]))

    with pytest.raises(SplitError, match="not before the first validation fight"):
        assert_no_leak(dates, bad)
