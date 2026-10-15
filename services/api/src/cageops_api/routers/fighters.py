"""GET /fighters (search) and GET /fighters/{id} (profile and record)."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from cageops_api import queries
from cageops_api.db import Database
from cageops_api.errors import ApiError, ErrorBody
from cageops_api.schemas import (
    FighterProfile,
    FighterSearchResponse,
    FighterSummary,
    OpponentRef,
    RecentFight,
    Record,
)
from cageops_common.inference.asof import app_today

router = APIRouter(tags=["fighters"])

MAX_LIMIT = 50


def get_database(request: Request) -> Database:
    return request.app.state.database


def whole_years(born: date | None, on: date) -> int | None:
    if born is None:
        return None
    return on.year - born.year - ((on.month, on.day) < (born.month, born.day))


def result_for(row: dict, fighter_id: int) -> str:
    outcome = row["outcome"]
    if outcome == "win":
        return "win" if row["winner_id"] == fighter_id else "loss"
    return outcome if outcome in ("draw", "no_contest") else "unknown"


@router.get(
    "/fighters",
    response_model=FighterSearchResponse,
    summary="Search fighters by name",
)
def search_fighters(
    db: Annotated[Database, Depends(get_database)],
    search: Annotated[
        str,
        Query(min_length=2, max_length=100, description="Part of a name; case is ignored."),
    ],
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 20,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> FighterSearchResponse:
    """Names containing the text, exact match first, then names that start with it, then the
    rest alphabetically. `%` and `_` in the text mean themselves. Accents are not folded."""
    with db.connect() as conn:
        rows, total = queries.search_fighters(conn, search.strip(), limit=limit, offset=offset)
    return FighterSearchResponse(
        items=[FighterSummary(**row) for row in rows], total=total, limit=limit, offset=offset
    )


@router.get(
    "/fighters/{fighter_id}",
    response_model=FighterProfile,
    summary="One fighter: profile, record and last five fights",
    responses={404: {"model": ErrorBody, "description": "No such fighter."}},
)
def get_fighter(
    request: Request,
    db: Annotated[Database, Depends(get_database)],
    fighter_id: int,
) -> FighterProfile:
    clock = request.app.state.clock
    today = app_today(request.app.state.settings.app_timezone, now=clock())
    with db.connect() as conn:
        profile = queries.fighter_profile(conn, fighter_id)
        if profile is None:
            raise ApiError(404, "fighter_not_found", f"no fighter with id {fighter_id}")
        record = queries.fighter_record(conn, fighter_id)
        recent = queries.recent_fights(conn, fighter_id)
    return FighterProfile(
        id=profile["id"],
        name=profile["name"],
        date_of_birth=profile["dob"],
        age_years=whole_years(profile["dob"], today),
        height_cm=profile["height_cm"],
        reach_cm=profile["reach_cm"],
        stance=profile["stance"],
        weight_class=profile["weight_class"],
        record=Record(**record),
        last_fights=[
            RecentFight(
                event_date=r["event_date"],
                event_name=r["event_name"],
                opponent=OpponentRef(id=r["opponent_id"], name=r["opponent_name"]),
                result=result_for(r, fighter_id),  # type: ignore[arg-type]
                method=r["method"],
                finish_round=r["finish_round"],
                is_title_fight=r["is_title_fight"],
                weight_class=r["weight_class"],
            )
            for r in recent
        ],
    )
