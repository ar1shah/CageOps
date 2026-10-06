"""The worker's pulse and its small HTTP server: /healthz and /metrics (D-024).

/healthz answers one question for Kubernetes: "is this worker's loop still moving?". The answer is a
pulse, a timestamp the loop refreshes every time it looks at the queue or finishes a job. If the
pulse is older than the longest job is allowed to run (plus a margin), the worker is wedged and
should be replaced. Nothing else changes the status code: Redis down, Postgres down or a blocked
site are real problems, but restarting the worker fixes none of them (and a Redis blip would
restart every worker at once). Those facts go in the JSON body and in /metrics instead.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, CollectorRegistry, generate_latest

from cageops_worker.ingest.metrics import WORKER_HOLD, WORKER_PULSE_AGE
from cageops_worker.ingest.observe import Observer

log = logging.getLogger(__name__)


class Heartbeat:
    """The pulse. beat() is a plain float write, so it is safe to call from any thread."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._last = clock()

    def beat(self) -> None:
        self._last = self._clock()

    def age(self) -> float:
        return self._clock() - self._last


class WorkerStatus:
    """What /healthz reports about this one worker process."""

    def __init__(self, heartbeat: Heartbeat, max_age_s: float):
        self.heartbeat = heartbeat
        self.max_age_s = max_age_s
        self.state = "starting"  # starting | running | hold:<reason> | stopping

    def set_state(self, state: str) -> None:
        self.state = state
        WORKER_HOLD.clear()
        if state.startswith("hold:"):
            WORKER_HOLD.labels(reason=state.removeprefix("hold:")).set(1)

    def alive(self) -> bool:
        return self.heartbeat.age() <= self.max_age_s


def health_response(status: WorkerStatus, observer: Observer) -> tuple[int, dict[str, Any]]:
    """(HTTP status, JSON body). Pure reads of in-memory state: no I/O, so it can't hang."""
    snap = observer.snapshot()
    age = observer.age()

    def word(ok: bool | None) -> str:
        return "unknown" if ok is None else ("ok" if ok else "down")

    body = {
        "status": "ok" if status.alive() else "stale",
        "worker_state": status.state,
        "pulse_age_s": round(status.heartbeat.age(), 1),
        "max_pulse_age_s": status.max_age_s,
        "dependencies": {
            "redis": word(snap.redis_ok),
            "postgres": word(snap.postgres_ok),
            "checked_age_s": None if age is None else round(age, 1),
        },
        "breaker": (
            None
            if snap.breaker is None
            else {"reason": snap.breaker.reason, "since": snap.breaker.detected_at}
        ),
        "queue": snap.queue_depth,
        "dead_letters": None if snap.dead_letters is None else sum(snap.dead_letters.values()),
    }
    return (200 if status.alive() else 503), body


class HealthServer:
    def __init__(
        self,
        host: str,
        port: int,
        status: WorkerStatus,
        observer: Observer,
        registry: CollectorRegistry = REGISTRY,
    ):
        WORKER_PULSE_AGE.set_function(status.heartbeat.age)
        handler = _handler(status, observer, registry)
        self._server = ThreadingHTTPServer((host, port), handler)
        self._server.daemon_threads = True
        self._started = False
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.05},
            name="http",
            daemon=True,
        )

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def start(self) -> None:
        self._started = True
        self._thread.start()
        log.info("worker http server listening", extra={"port": self.port})

    def stop(self) -> None:
        if self._started:  # shutdown() would wait forever for a loop that never ran
            self._server.shutdown()
        self._server.server_close()


def _handler(
    status: WorkerStatus, observer: Observer, registry: CollectorRegistry
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/healthz":
                code, body = health_response(status, observer)
                self._send(code, "application/json", json.dumps(body).encode())
            elif path == "/metrics":
                self._send(200, CONTENT_TYPE_LATEST, generate_latest(registry))
            else:
                self._send(404, "application/json", b'{"error": "not found"}')

        def _send(self, code: int, content_type: str, payload: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass  # one line per probe would drown the job logs

    return Handler
