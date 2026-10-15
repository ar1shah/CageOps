"""The CageOps prediction API.

Run it: `uv run --package cageops-api uvicorn cageops_api.main:app --port 8000 --no-proxy-headers
--no-access-log` (we log requests ourselves, as JSON, and decide whom to trust as a proxy).

Nothing here reads the environment at import time; `create_app` reads it when the app starts.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import Engine

from cageops_api.config import ApiSettings, get_api_settings
from cageops_api.metrics import ApiMetrics
from cageops_api.middleware import RequestContextMiddleware
from cageops_api.model_manager import ModelManager, ModelUnavailable
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
    configure_logs: bool = True,
    poll: bool = True,
) -> FastAPI:
    """Build the app. Arguments exist for tests; the real app passes none and reads the
    environment inside the lifespan, not here. `poll=False` starts no background poller (a test
    then calls `app.state.model_manager.refresh()` itself)."""
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
            common.database_url, pool_size=5, max_overflow=5, pool_pre_ping=True
        )
        app.state.engine = owned_engine
        manager = ModelManager(
            owned_engine,
            own.model_artifact_dir,
            poll_interval_s=own.model_poll_interval_s,
            retry_s=own.model_reload_retry_s,
            metrics=metrics,
        )
        app.state.model_manager = manager
        # Load before taking traffic. A failure does not stop the app: it starts, reports not
        # ready (503 on /readyz and on predictions), and the poller keeps trying.
        await asyncio.to_thread(manager.refresh)
        task = asyncio.create_task(manager.poll_forever()) if poll else None
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
            if engine is None:
                owned_engine.dispose()

    app = FastAPI(
        title="CageOps API",
        version="0.1.0",
        description=DESCRIPTION,
        lifespan=lifespan,
    )
    app.state.metrics = metrics
    app.add_middleware(RequestContextMiddleware, metrics=metrics)

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
