"""One shared view of "is Redis answering?", so the cache and the rate limiter fail open together.

Every Redis call in the API is optional: the cache can be recomputed and the limiter can allow.
When one fails, the other would fail the same way a moment later, so they share this guard: a
failure is counted, logged at most once per window, and Redis is left alone for a few seconds so a
dead Redis does not charge every request its timeout.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import redis

from cageops_api.metrics import ApiMetrics

log = logging.getLogger(__name__)

# What counts as "Redis is unavailable". OSError covers a socket that dies below redis-py.
FAIL_OPEN = (redis.exceptions.RedisError, OSError)


class RedisGuard:
    def __init__(
        self,
        metrics: ApiMetrics,
        *,
        clock: Callable[[], float] = time.monotonic,
        skip_s: float = 5.0,
        log_every_s: float = 30.0,
    ) -> None:
        self.metrics = metrics
        self._clock = clock
        self._skip_s = skip_s
        self._log_every_s = log_every_s
        self._skip_until = 0.0
        self._last_logged = float("-inf")

    def available(self) -> bool:
        """False during the no-ask window after a failure."""
        return self._clock() >= self._skip_until

    def failed(self, op: str, exc: Exception) -> None:
        now = self._clock()
        self.metrics.redis_errors.labels(op).inc()
        self._skip_until = now + self._skip_s
        if now - self._last_logged >= self._log_every_s:
            self._last_logged = now
            log.warning(
                "redis unavailable; failing open",
                extra={"op": op, "error": type(exc).__name__, "skipping_for_s": self._skip_s},
            )
