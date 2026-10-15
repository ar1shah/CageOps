"""Time-based splits, and the check that proves no training fight is from the validation era.

Never a random split (rule 4). A model is trained on older fights, early-stopped on the next
year, and graded once on the most recent year. Boundaries are dates, so an event never straddles
two splits.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np


class SplitError(ValueError):
    """The requested boundaries cannot give three non-empty, correctly ordered splits."""


@dataclass(frozen=True)
class Boundaries:
    val_start: date
    test_start: date
    train_start: date | None = None  # None: all history

    def as_dict(self) -> dict[str, str | None]:
        return {
            "train_start": self.train_start.isoformat() if self.train_start else None,
            "val_start": self.val_start.isoformat(),
            "test_start": self.test_start.isoformat(),
        }


@dataclass(frozen=True)
class SplitIndices:
    train: np.ndarray
    val: np.ndarray
    test: np.ndarray


def one_year_before(day: date) -> date:
    try:
        return day.replace(year=day.year - 1)
    except ValueError:  # 29 February
        return day.replace(year=day.year - 1, day=28)


def default_boundaries(last_event: date, train_start: date | None = None) -> Boundaries:
    """Test is the last 365 days of data and validation the 365 days before that."""
    test_start = one_year_before(last_event) + timedelta(days=1)
    return Boundaries(
        val_start=one_year_before(test_start), test_start=test_start, train_start=train_start
    )


def make_splits(event_dates: Sequence[date], boundaries: Boundaries) -> SplitIndices:
    """Index arrays into `event_dates`. Raises SplitError rather than returning a bad split."""
    if boundaries.val_start >= boundaries.test_start:
        raise SplitError(
            f"val_start {boundaries.val_start} must be before test_start {boundaries.test_start}"
        )
    if boundaries.train_start and boundaries.train_start >= boundaries.val_start:
        raise SplitError("train_start must be before val_start")

    dates = np.array(event_dates, dtype="datetime64[D]")
    val_start = np.datetime64(boundaries.val_start)
    test_start = np.datetime64(boundaries.test_start)
    train_mask = dates < val_start
    if boundaries.train_start:
        train_mask &= dates >= np.datetime64(boundaries.train_start)
    splits = SplitIndices(
        train=np.flatnonzero(train_mask),
        val=np.flatnonzero((dates >= val_start) & (dates < test_start)),
        test=np.flatnonzero(dates >= test_start),
    )
    for name, indices in (("train", splits.train), ("val", splits.val), ("test", splits.test)):
        if len(indices) == 0:
            raise SplitError(f"the {name} split has no fights with boundaries {boundaries}")
    assert_no_leak(event_dates, splits)
    return splits


def assert_no_leak(event_dates: Sequence[date], splits: SplitIndices) -> None:
    """No training fight is dated on or after the first validation fight, and no validation
    fight on or after the first test fight. Checked on the dates themselves, not the boundaries,
    so it still holds if someone changes how the boundaries are computed."""
    dates = list(event_dates)
    last_train = max(dates[i] for i in splits.train)
    first_val = min(dates[i] for i in splits.val)
    last_val = max(dates[i] for i in splits.val)
    first_test = min(dates[i] for i in splits.test)
    if last_train >= first_val:
        raise SplitError(
            f"a training fight ({last_train}) is not before the first validation fight"
        )
    if last_val >= first_test:
        raise SplitError(f"a validation fight ({last_val}) is not before the first test fight")


def describe(event_dates: Sequence[date], indices: np.ndarray) -> dict[str, object]:
    dates = [event_dates[i] for i in indices]
    return {"n": len(dates), "first": min(dates).isoformat(), "last": max(dates).isoformat()}
