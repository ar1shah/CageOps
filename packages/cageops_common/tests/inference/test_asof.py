"""What "today" is, and the two date-relative features. Stdlib only: no numpy, no database."""

import subprocess
import sys
from datetime import UTC, date, datetime

import pytest

from cageops_common.inference.asof import DEFAULT_TIMEZONE, app_today, date_relative_features


def test_the_default_zone_is_new_york():
    assert DEFAULT_TIMEZONE == "America/New_York"


@pytest.mark.parametrize(
    ("utc", "expected"),
    [
        # EDT (UTC-4), October: midnight in New York is 04:00Z
        (datetime(2026, 10, 15, 3, 30, tzinfo=UTC), date(2026, 10, 14)),
        (datetime(2026, 10, 15, 3, 59, 59, tzinfo=UTC), date(2026, 10, 14)),
        (datetime(2026, 10, 15, 4, 0, 0, tzinfo=UTC), date(2026, 10, 15)),
        (datetime(2026, 10, 15, 20, 0, tzinfo=UTC), date(2026, 10, 15)),
        # EST (UTC-5), December: midnight in New York is 05:00Z (the offset moved with DST)
        (datetime(2026, 12, 15, 4, 59, 59, tzinfo=UTC), date(2026, 12, 14)),
        (datetime(2026, 12, 15, 5, 0, 0, tzinfo=UTC), date(2026, 12, 15)),
    ],
)
def test_today_is_the_new_york_calendar_date_not_the_utc_one(utc, expected):
    assert app_today(now=utc) == expected


def test_a_saturday_night_card_is_still_today_after_utc_has_rolled_over():
    """UFC 332-style card: 11 pm ET Saturday is already Sunday in UTC. A US event dated Saturday
    must still be "today" (so its still-scheduled bouts are upcoming, not history)."""
    saturday_11pm_et = datetime(2026, 10, 18, 3, 0, tzinfo=UTC)  # = Sat 2026-10-17 23:00 EDT
    assert saturday_11pm_et.date() == date(2026, 10, 18)  # UTC says Sunday
    assert app_today(now=saturday_11pm_et) == date(2026, 10, 17)


def test_another_zone_can_be_chosen():
    assert app_today("UTC", now=datetime(2026, 10, 15, 3, 30, tzinfo=UTC)) == date(2026, 10, 15)


def test_a_naive_datetime_is_refused():
    with pytest.raises(ValueError, match="timezone-aware"):
        app_today(now=datetime(2026, 10, 15, 3, 30))


def test_age_and_days_since_last_fight_are_differences_from_the_as_of_date():
    got = date_relative_features(date(2026, 10, 15), date(1990, 1, 1), date(2026, 5, 16))

    assert got == {
        "age_days": (date(2026, 10, 15) - date(1990, 1, 1)).days,
        "days_since_last_fight": 152,
    }


def test_each_missing_input_gives_none_never_zero():
    assert date_relative_features(date(2026, 10, 15), None, date(2026, 10, 14)) == {
        "age_days": None,
        "days_since_last_fight": 1,
    }
    assert (
        date_relative_features(date(2026, 10, 15), date(1990, 1, 1), None)["days_since_last_fight"]
        is None
    )


def test_the_same_inputs_one_day_later_move_both_by_exactly_one():
    today = date_relative_features(date(2026, 10, 15), date(1990, 1, 1), date(2026, 5, 16))
    tomorrow = date_relative_features(date(2026, 10, 16), date(1990, 1, 1), date(2026, 5, 16))

    assert tomorrow["age_days"] == today["age_days"] + 1
    assert tomorrow["days_since_last_fight"] == today["days_since_last_fight"] + 1


def test_importing_it_does_not_load_numpy_or_lightgbm():
    """The worker imports this module; it must not drag the inference extra in."""
    code = (
        "import sys; import cageops_common.inference.asof, cageops_common.config; "
        "bad = [m for m in ('numpy', 'lightgbm') if m in sys.modules]; assert not bad, bad"
    )
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
