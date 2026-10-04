"""A background look at the things around the worker: Redis, Postgres, the circuit breaker, the
queue and the dead-letter queue.

/healthz and /metrics must answer instantly, even while Redis or Postgres is down, because a
Kubernetes probe that times out counts as a failure. So nothing in a request ever talks to Redis or
Postgres. Instead one thread, with its own connections and short timeouts, takes a snapshot every
few seconds, and requests just read the latest one. Like a shop window display: it is refreshed
from the stockroom regularly, and passers-by look at the window instead of walking in.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from redis import Redis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.retry import Retry
from rq import Queue
from rq.registry import FailedJobRegistry, ScheduledJobRegistry, StartedJobRegistry
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from cageops_scraper.breaker import BreakerState, CircuitBreaker
from cageops_worker.ingest import dlq

log = logging.getLogger(__name__)


def probe_redis(url: str, timeout_s: float) -> Redis:
    """A Redis client for looking, not working: short timeouts and no retries, so a dead Redis is
    reported in about `timeout_s` instead of after redis-py's own multi-second retry dance.
    Not decoded: RQ's registries need bytes."""
    return Redis.from_url(
        url,
        socket_timeout=timeout_s,
        socket_connect_timeout=timeout_s,
        retry=Retry(NoBackoff(), 0),
    )


@dataclass(frozen=True)
class Snapshot:
    """What the observer last saw. None means "couldn't tell", which is not the same as zero."""

    taken_at: float | None = None  # monotonic seconds; None before the first look
    redis_ok: bool | None = None
    postgres_ok: bool | None = None
    breaker_open: bool | None = None
    breaker: BreakerState | None = None
    queue_depth: dict[str, int] | None = None  # queued / scheduled / started / failed
    dead_letters: dict[str, int] | None = None  # by reason


class Observer:
    def __init__(
        self,
        redis: Redis,
        engine: Engine,
        queue_name: str,
        source_name: str,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.redis = redis  # NOT the worker's connection: RQ gives that one a long socket timeout
        self.engine = engine
        self.queue = Queue(queue_name, connection=redis)
        self.queue_name = queue_name
        self.source_name = source_name
        self._breaker = CircuitBreaker(redis, source_name)
        self._clock = clock
        self._snapshot = Snapshot()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def snapshot(self) -> Snapshot:
        return self._snapshot

    def age(self) -> float | None:
        taken = self._snapshot.taken_at
        return None if taken is None else self._clock() - taken

    def refresh(self) -> Snapshot:
        """Take a fresh look now. Never raises: a failed look is recorded as "down" or unknown."""
        redis_ok = self._redis_ok()
        postgres_ok = self._postgres_ok()
        breaker_open = breaker = depth = letters = None
        if redis_ok:
            try:
                state = self._breaker.state()
                breaker, breaker_open = state, state is not None
                depth = self._queue_depth()
                letters = dict(dlq.summary(self.queue))
            except RedisError as exc:  # went down between the ping and now
                log.warning("observer: redis failed mid-look: %s", exc)
                redis_ok = False
        self._snapshot = Snapshot(
            self._clock(), redis_ok, postgres_ok, breaker_open, breaker, depth, letters
        )
        return self._snapshot

    def _redis_ok(self) -> bool:
        try:
            return bool(self.redis.ping())
        except RedisError:
            return False

    def _postgres_ok(self) -> bool:
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except SQLAlchemyError:
            return False

    def _queue_depth(self) -> dict[str, int]:
        # zcard, not the registries' .count: counting a registry also cleans it up (moves expired
        # jobs around), and a monitoring read must not change anything.
        def size(registry: Any) -> int:
            return int(self.redis.zcard(registry.key))

        return {
            "queued": int(self.redis.llen(self.queue.key)),
            "scheduled": size(ScheduledJobRegistry(queue=self.queue)),
            "started": size(StartedJobRegistry(queue=self.queue)),
            "failed": size(FailedJobRegistry(queue=self.queue)),
        }

    def start(self, interval_s: float) -> None:
        """Refresh every `interval_s` seconds in a daemon thread (it never blocks shutdown)."""

        def loop() -> None:
            while True:
                try:
                    self.refresh()
                except Exception:  # a monitoring thread must outlive anything it hits
                    log.exception("observer: refresh failed")
                if self._stop.wait(interval_s):
                    return

        self._thread = threading.Thread(target=loop, name="observer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
