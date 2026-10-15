"""POST /predict."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from cageops_api.cache import PredictionCache, cache_key
from cageops_api.db import Database
from cageops_api.errors import ErrorBody
from cageops_api.features_watch import FeaturesWatch
from cageops_api.metrics import ApiMetrics
from cageops_api.model_manager import ModelManager
from cageops_api.predict import PredictionCore, compute_core, orient, record_prediction, stage
from cageops_api.routers.fighters import get_database
from cageops_api.schemas import PredictRequest, PredictResponse
from cageops_common.inference.asof import app_today

router = APIRouter(tags=["predictions"])


@router.post(
    "/predict",
    response_model=PredictResponse,
    summary="Win probability, how the fight ends, and what drove it",
    responses={
        404: {"model": ErrorBody, "description": "A fighter id does not exist."},
        422: {
            "model": ErrorBody,
            "description": "Same fighter twice, a bad value, or a weight class that must be "
            "given because the fighters' recent divisions differ.",
        },
        503: {
            "model": ErrorBody,
            "description": "No verified model is loaded, or no features exist for a fighter.",
        },
    },
)
def predict(
    body: PredictRequest, request: Request, db: Annotated[Database, Depends(get_database)]
) -> JSONResponse:
    """Estimates for fighter A against fighter B, **as of today**: only completed fights strictly
    before today are used (the same point-in-time rule as the training data).

    - `win_probability` sums to 1, and asking for B against A returns the mirror image.
    - `method_probabilities` is how the fight ends, whoever wins.
    - `top_features` are the model inputs that moved the estimate most, in log-odds, from A's
      side.
    - `data_quality` says whether the fighters' features include every completed fight
      (`features_current`) and whether their rates include every fight's per-round stats
      (`rates_complete`). A prediction with a gap is still returned; it is never called fresh.

    Of the context fields only `weight_class` is used by the model today; `is_title_fight` and
    `scheduled_rounds` are validated and echoed (`matchup.context_used_by_model`).

    Not betting advice. On a year of held-out fights the model did not beat the betting favorite.
    """
    app = request.app
    manager: ModelManager = app.state.model_manager
    metrics: ApiMetrics = app.state.metrics
    cache: PredictionCache = app.state.cache
    watch: FeaturesWatch = app.state.features_watch
    serving = manager.require()  # one reference, used for this whole request (D-036)
    as_of = app_today(app.state.settings.app_timezone, now=app.state.clock())
    top_n = app.state.api_settings.predict_top_features

    # The key is built from the request and from facts polled in the background (model version,
    # features build, input fingerprint), so a hit costs no database round trip at all.
    features = watch.state
    key: str | None = None
    core: PredictionCore | None = None
    status = "bypass"
    if cache.enabled and features is not None:
        title = bool(body.is_title_fight)
        key = cache_key(
            model_version=serving.version,
            build_id=features.build_id,
            inputs_sha12=features.inputs_sha12,
            fighter_a_id=body.fighter_a_id,
            fighter_b_id=body.fighter_b_id,
            weight_class=body.weight_class,
            scheduled_rounds=body.scheduled_rounds or (5 if title else 3),
            is_title_fight=title,
            top_n=top_n,
            as_of=as_of,
        )
        with stage(metrics, "cache"):
            core, status = cache.get(key)
        if core is not None and core.model_version != serving.version:
            core, status = None, "miss"  # belt and braces: the key already says so
    else:
        metrics.cache_bypassed.inc()

    if core is None:
        core = compute_core(
            request=body, db=db, serving=serving, as_of=as_of, top_n=top_n, metrics=metrics
        )
        if key is not None:
            with stage(metrics, "cache"):
                cache.set(key, core)

    record_prediction(metrics, core)
    response = orient(core, body.fighter_a_id)
    with stage(metrics, "serialize"):
        return JSONResponse(content=response.model_dump(mode="json"), headers={"X-Cache": status})
