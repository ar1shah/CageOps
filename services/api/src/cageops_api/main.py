"""The CageOps prediction API.

Run it: `uv run --package cageops-api uvicorn cageops_api.main:app --port 8000 --no-proxy-headers
--no-access-log` (we log requests ourselves, as JSON, and decide whom to trust as a proxy).

Nothing here reads the environment at import time; `create_app` reads it when the app starts.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import redis
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from redis.backoff import NoBackoff
from redis.retry import Retry
from sqlalchemy import Engine

from cageops_api.cache import PredictionCache
from cageops_api.config import ApiSettings, get_api_settings
from cageops_api.db import Database
from cageops_api.errors import install_error_handlers
from cageops_api.features_watch import FeaturesWatch
from cageops_api.metrics import ApiMetrics
from cageops_api.middleware import RequestContextMiddleware
from cageops_api.model_manager import ModelManager, ModelUnavailable
from cageops_api.ratelimit import (
    RateLimitConfig,
    RateLimiter,
    RateLimitMiddleware,
    RouteTemplates,
)
from cageops_api.redis_guard import RedisGuard
from cageops_api.routers import events, fighters, predict
from cageops_common.config import Settings, get_settings
from cageops_common.db.session import make_engine
from cageops_common.logs import configure_logging

log = logging.getLogger(__name__)

DESCRIPTION = """
Predicted outcomes for UFC fights: win probability for each fighter, how the fight is likely to
end, and which features drove the estimate.

**These are a model's estimates, not betting advice**, and no response carries odds. On a year of
held-out fights the model did not beat the betting favorite (docs/BENCHMARKS.md).
"""


def create_app(
    *,
    settings: Settings | None = None,
    api_settings: ApiSettings | None = None,
    engine: Engine | None = None,
    redis_client: redis.Redis | None = None,
    configure_logs: bool = True,
    poll: bool = True,
    clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    """Build the app. Arguments exist for tests; the real app passes none and reads the
    environment inside the lifespan, not here. `redis_client` replaces the one built from
    REDIS_URL (tests). `poll=False` starts no background poller (a test then calls
    `app.state.model_manager.refresh()` and `app.state.features_watch.refresh()` itself). `clock`
    returns an aware datetime; it is how tests pin "today"."""
    metrics = ApiMetrics()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        common = settings or get_settings()
        if configure_logs:
            configure_logging(common.log_level)
            # uvicorn installs its own handlers; route its records through ours (one JSON stream).
            for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
                logger = logging.getLogger(name)
                logger.handlers.clear()
                logger.propagate = True
        own = api_settings or get_api_settings()
        app.state.settings = common
        app.state.api_settings = own
        owned_engine = engine or make_engine(
            common.database_url,
            pool_size=own.db_pool_size,
            max_overflow=own.db_max_overflow,
            pool_timeout=own.db_pool_timeout_s,
            pool_pre_ping=True,
        )
        app.state.engine = owned_engine
        app.state.database = Database(owned_engine, metrics)
        manager = ModelManager(
            owned_engine,
            own.model_artifact_dir,
            poll_interval_s=own.model_poll_interval_s,
            retry_s=own.model_reload_retry_s,
            metrics=metrics,
        )
        app.state.model_manager = manager
        watch = FeaturesWatch(owned_engine, metrics)
        app.state.features_watch = watch

        owned_redis = redis_client
        if owned_redis is None and (own.cache_enabled or own.rate_limit_enabled):
            timeout = own.redis_timeout_ms / 1000
            owned_redis = redis.Redis.from_url(
                common.redis_url,
                socket_timeout=timeout,
                socket_connect_timeout=timeout,
                retry=Retry(NoBackoff(), 0),  # fail fast: the cache must never be slower than not
                health_check_interval=0,
            )
        guard = RedisGuard(metrics)  # one view of Redis's health for the cache and the limiter
        app.state.redis_guard = guard
        app.state.cache = PredictionCache(
            owned_redis if own.cache_enabled else None,
            ttl_s=own.cache_ttl_s,
            metrics=metrics,
            guard=guard,
        )
        app.state.rate_limiter = None
        app.state.route_templates = RouteTemplates.from_app(app)
        if own.rate_limit_enabled and owned_redis is not None:
            keys = own.frontend_keys()
            configured = bool(
                own.frontend_api_key.get_secret_value()
                or own.frontend_api_key_previous.get_secret_value()
            )
            if configured and not keys:
                log.warning(
                    "a frontend API key is set but shorter than 32 characters; "
                    "the frontend client class is disabled"
                )
            app.state.rate_limiter = RateLimiter(
                owned_redis,
                config=RateLimitConfig(
                    enabled=True,
                    per_minute=own.rate_limit_per_minute,
                    burst=own.rate_limit_burst,
                    trusted_proxies=own.trusted_proxies(),
                    frontend_keys=keys,
                    user_per_minute=own.frontend_user_rate_per_minute,
                    user_burst=own.frontend_user_burst,
                    key_per_minute=own.frontend_key_rate_per_minute,
                    key_burst=own.frontend_key_burst,
                ),
                metrics=metrics,
                guard=guard,
            )

        # Load before taking traffic. A failure does not stop the app: it starts, reports not
        # ready (503 on /readyz and on predictions), and the poller keeps trying.
        await asyncio.to_thread(manager.refresh)
        await asyncio.to_thread(watch.refresh)

        async def poll_forever() -> None:
            while True:
                await asyncio.sleep(own.model_poll_interval_s)
                await asyncio.to_thread(manager.refresh)
                await asyncio.to_thread(watch.refresh)

        task = asyncio.create_task(poll_forever()) if poll else None
        log.info(
            "api started",
            extra={
                "timezone": common.app_timezone,
                "model_version": manager.current.version if manager.current else None,
                "ready": manager.readiness()["ready"],
            },
        )
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if redis_client is None and owned_redis is not None:
                owned_redis.close()
            if engine is None:
                owned_engine.dispose()

    app = FastAPI(
        title="CageOps API",
        version="0.1.0",
        description=DESCRIPTION,
        lifespan=lifespan,
    )
    app.state.metrics = metrics
    app.state.clock = clock or (lambda: datetime.now(UTC))
    metrics.watch_pool(lambda: getattr(app.state, "engine", None))
    # Added last is outermost: the request context wraps the limiter, so a 429 is timed,
    # labelled with its route template and logged like any other response.
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(RequestContextMiddleware, metrics=metrics)
    install_error_handlers(app)
    app.include_router(fighters.router)
    app.include_router(predict.router)
    app.include_router(events.router)

    @app.exception_handler(ModelUnavailable)
    async def model_unavailable(_: Request, exc: ModelUnavailable) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"detail": f"no model is available: {exc.reason}", "code": "model_unavailable"},
        )

    @app.get("/healthz", tags=["ops"], summary="Liveness: the process is up")
    def healthz() -> dict[str, str]:
        """Always 200 while the process answers. It does no I/O on purpose: a database blip
        must not make an orchestrator restart a healthy process."""
        return {"status": "ok"}

    @app.get("/readyz", tags=["ops"], summary="Readiness: able to serve predictions")
    def readyz(request: Request, response: Response) -> dict[str, object]:
        """200 only when a verified model is loaded and the database is reachable; 503 with the
        reason otherwise. Redis is reported but never decides readiness (the API fails open)."""
        manager: ModelManager | None = getattr(request.app.state, "model_manager", None)
        if manager is None:
            response.status_code = 503
            return {"ready": False, "reason": "the model manager has not started"}
        status = manager.readiness()
        if not status["ready"]:
            response.status_code = 503
        return status

    @app.get("/metrics", tags=["ops"], include_in_schema=False)
    def prometheus_metrics() -> Response:
        return Response(generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)

    return app


app = create_app()
