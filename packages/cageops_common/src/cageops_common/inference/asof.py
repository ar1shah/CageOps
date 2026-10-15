"""What "today" is, and the two features that depend on the calendar rather than on fight history.

Stdlib only, on purpose: the worker's feature builder and the API both import this, and the worker
must not pull in numpy or LightGBM. (D-037)

Two of the 34 model features are functions of the date a prediction is *for*:

    age_days              = as_of - dob
    days_since_last_fight = as_of - date of the fighter's latest completed fight before as_of

Everything else is a function of the fights strictly before `as_of` (D-027). So a stored feature
row built yesterday is right about history and wrong about these two, unless they are derived
again from stored inputs (`dob`, `last_fight_date`) for the date being asked about. The pipeline
(`compute_features`) and the API call the same function, so the rule is written once.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

# Event dates on ufcstats and Wikipedia are US-local calendar dates and cards run into the late
# evening, so a US zone is the one under which "an event dated D" means "today until midnight".
DEFAULT_TIMEZONE = "America/New_York"


def app_today(timezone: str = DEFAULT_TIMEZONE, *, now: datetime | None = None) -> date:
    """The calendar date in `timezone`. `now` (an aware datetime) is for tests; production calls
    it with no arguments and reads the clock."""
    moment = now if now is not None else datetime.now(UTC)
    if moment.tzinfo is None:
        raise ValueError("`now` must be timezone-aware")
    return moment.astimezone(ZoneInfo(timezone)).date()


def date_relative_features(
    as_of: date, dob: date | None, last_fight_date: date | None
) -> dict[str, int | None]:
    """`age_days` and `days_since_last_fight` for a prediction made as of `as_of`.

    NULL when an input is NULL (a fighter with no bio date, or no prior fight), never 0.
    `last_fight_date` must already be the fighter's latest completed fight *before* `as_of`."""
    return {
        "days_since_last_fight": (as_of - last_fight_date).days if last_fight_date else None,
        "age_days": (as_of - dob).days if dob else None,
    }
