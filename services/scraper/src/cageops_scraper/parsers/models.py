"""The typed forms the parsers fill in. Each is frozen (can't change after parsing) and rejects
unknown fields, so a typo in a parser is an error instead of a silently dropped value.

These hold what the PAGE says, in the site's own words ("Decision - Unanimous", "U-DEC"). Turning
that into our database vocabulary happens later, with the same functions the seed loader uses.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict


class Parsed(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EventListRow(Parsed):
    """One row of the events list (completed or upcoming)."""

    ufcstats_id: str
    url: str
    name: str
    event_date: date
    location_raw: str | None
    # The completed list's first row is the NEXT upcoming event, marked with a "next" icon.
    # It is not completed; callers must not treat the list it came from as proof of that.
    is_next_marker: bool
