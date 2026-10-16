"""One pass around every request: a request id, timing, the latency histogram and an access line.

Written as a plain ASGI middleware, not `BaseHTTPMiddleware`: that one runs the app in a separate
task, which breaks context variables (the request id must reach log lines emitted inside the
handler's thread) and buffers streaming responses.

The `route` label is the route TEMPLATE (`/fighters/{fighter_id}`), read after routing has run.
A raw path as a label would make one time series per fighter id, and one per URL a scanner tries.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from cageops_api.metrics import UNMATCHED, ApiMetrics
from cageops_common.logs import log_context

REQUEST_ID_HEADER = "x-request-id"
# What an inbound id may look like. Anything else is replaced: the value ends up in every log line
# and in a response header, so it must not be able to carry newlines or markup.
_VALID_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")

access_log = logging.getLogger("cageops_api.access")


def route_template(scope: Scope) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else UNMATCHED


def client_ip(scope: Scope) -> str | None:
    """The address the rate limiter attributed the request to, else the socket peer."""
    attributed = (scope.get("cageops_client") or {}).get("ip")
    if attributed:
        return attributed
    client = scope.get("client")
    return client[0] if client else None


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp, metrics: ApiMetrics) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = self._request_id(scope)
        state: dict[str, Any] = {"status": 500}  # 500 if the app dies before sending anything
        started = time.perf_counter()

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        self.metrics.in_flight.inc()
        try:
            with log_context(request_id=request_id):
                try:
                    await self.app(scope, receive, send_with_id)
                finally:
                    elapsed = time.perf_counter() - started
                    route = route_template(scope)
                    self.metrics.request_seconds.labels(
                        scope["method"], route, str(state["status"])
                    ).observe(elapsed)
                    access_log.info(
                        "request",
                        extra={
                            "method": scope["method"],
                            "route": route,
                            "status": state["status"],
                            "duration_ms": round(elapsed * 1000, 2),
                            "client_ip": client_ip(scope),
                            "client_class": (scope.get("cageops_client") or {}).get("class"),
                        },
                    )
        finally:
            self.metrics.in_flight.dec()

    @staticmethod
    def _request_id(scope: Scope) -> str:
        for name, value in scope.get("headers", []):
            if name == REQUEST_ID_HEADER.encode():
                candidate = value.decode("latin-1")
                if _VALID_ID.fullmatch(candidate):
                    return candidate
                break
        return uuid.uuid4().hex
