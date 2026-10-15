"""GET /events/upcoming: every scheduled bout on a card to come, predicted in one batch."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from cageops_api import queries
from cageops_api.cache import SOURCE_BOUT, PredictionCache, cache_key
from cageops_api.db import Database
from cageops_api.errors import ErrorBody
from cageops_api.features_watch import FeaturesWatch
from cageops_api.metrics import ApiMetrics
from cageops_api.model_manager import ModelManager
from cageops_api.predict import (
    PredictionCore,
    compute_bout_cores,
    data_quality_block,
    record_prediction,
    stage,
)
from cageops_api.routers.fighters import get_database
from cageops_api.schemas import (
    FighterRef,
    UpcomingBout,
    UpcomingEvent,
    UpcomingPrediction,
    UpcomingResponse,
    WinProbability,
)
from cageops_common.inference.asof import app_today

router = APIRouter(tags=["events"])


@router.get(
    "/events/upcoming",
    response_model=UpcomingResponse,
    summary="Every scheduled bout on an upcoming card, with a prediction",
    responses={503: {"model": ErrorBody, "description": "No verified model is loaded."}},
)
def upcoming(request: Request, db: Annotated[Database, Depends(get_database)]) -> JSONResponse:
    """Every `scheduled` bout on an event dated today or later, grouped by event.

    Each bout's features are as of its own event date. The predictions are made in one batch (one
    query per kind and one model call for all of them), and each one is cached under its own key.

    `is_title_fight` and `scheduled_rounds` are NULL: ufcstats does not publish them before the
    fight, and they are never defaulted (D-044). A bout with no features built yet is listed with
    `prediction: null` and the reason; it never fails the list.

    An empty database or no upcoming events is `{"events": []}`, not an error.
    """
    app = request.app
    manager: ModelManager = app.state.model_manager
    metrics: ApiMetrics = app.state.metrics
    cache: PredictionCache = app.state.cache
    watch: FeaturesWatch = app.state.features_watch
    serving = manager.require()
    today = app_today(app.state.settings.app_timezone, now=app.state.clock())
    top_n = app.state.api_settings.predict_top_features

    with stage(metrics, "db"), db.connect() as conn:
        bouts = queries.scheduled_bouts(conn, today)

    cores: dict[int, PredictionCore | str] = {}
    names: dict[int, str] = {}
    status = "bypass"
    features = watch.state
    keys: dict[int, str] = {}
    if bouts and cache.enabled and features is not None:
        for b in bouts:
            keys[b["fight_id"]] = cache_key(
                model_version=serving.version,
                build_id=features.build_id,
                inputs_sha12=features.inputs_sha12,
                fighter_a_id=b["fighter_a_id"],
                fighter_b_id=b["fighter_b_id"],
                weight_class=b["weight_class"],
                scheduled_rounds=b["scheduled_rounds"],
                is_title_fight=b["is_title_fight"],
                top_n=top_n,
                as_of=b["event_date"],
                source=SOURCE_BOUT,
            )
        with stage(metrics, "cache"):
            cached, status = cache.get_many([keys[b["fight_id"]] for b in bouts])
        for bout, core in zip(bouts, cached, strict=True):
            if core is not None and core.model_version == serving.version:
                cores[bout["fight_id"]] = core
    elif bouts:
        metrics.cache_bypassed.inc(len(bouts))

    missing = [b for b in bouts if b["fight_id"] not in cores]
    if missing:
        computed, names = compute_bout_cores(
            bouts=missing, db=db, serving=serving, top_n=top_n, metrics=metrics
        )
        cores.update(computed)
        if keys:
            fresh = [
                (keys[fid], core)
                for fid, core in computed.items()
                if isinstance(core, PredictionCore)
            ]
            with stage(metrics, "cache"):
                cache.set_many(fresh)

    with stage(metrics, "build"):
        events: dict[int, UpcomingEvent] = {}
        for b in bouts:
            event = events.setdefault(
                b["event_id"],
                UpcomingEvent(
                    event_id=b["event_id"],
                    name=b["event_name"],
                    event_date=b["event_date"],
                    bouts=[],
                ),
            )
            result = cores[b["fight_id"]]
            prediction = None
            reason = None
            if isinstance(result, PredictionCore):
                record_prediction(metrics, result, "upcoming")
                prediction = UpcomingPrediction(
                    win_probability=WinProbability(
                        fighter_a=result.p_lo, fighter_b=1.0 - result.p_lo
                    ),
                    method_probabilities=result.method,
                    data_quality=data_quality_block(result, result.quality_lo, result.quality_hi),
                )
            else:
                reason = result
            if isinstance(result, PredictionCore):
                fighter_a, fighter_b = result.lo, result.hi
            else:  # no prediction, so no core to take the names from; they were read with the rest
                fighter_a = FighterRef(id=b["fighter_a_id"], name=names[b["fighter_a_id"]])
                fighter_b = FighterRef(id=b["fighter_b_id"], name=names[b["fighter_b_id"]])
            event.bouts.append(
                UpcomingBout(
                    fight_id=b["fight_id"],
                    fighter_a=fighter_a,
                    fighter_b=fighter_b,
                    weight_class=b["weight_class"],
                    is_title_fight=b["is_title_fight"],
                    scheduled_rounds=b["scheduled_rounds"],
                    prediction=prediction,
                    unavailable_reason=reason,
                )
            )
    body = UpcomingResponse(
        as_of=today, model_version=serving.version, events=list(events.values())
    )
    with stage(metrics, "serialize"):
        return JSONResponse(content=body.model_dump(mode="json"), headers={"X-Cache": status})
