"""The API's one door to the database: a pooled connection, with the wait for it measured.

Every request-path query goes through `Database.connect()`. It times how long a connection took
to be handed over (the pool wait is invisible in a request histogram, and it is exactly what
saturates first under load) and turns "gave up waiting" into a 503 instead of a 500.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Connection, Engine
from sqlalchemy import exc as sa_exc

from cageops_api.errors import ApiError
from cageops_api.metrics import ApiMetrics


class DatabaseBusy(ApiError):
    def __init__(self) -> None:
        super().__init__(
            503,
            "database_busy",
            "no database connection became free in time; try again shortly",
            headers={"Retry-After": "1"},
        )


class Database:
    def __init__(self, engine: Engine, metrics: ApiMetrics) -> None:
        self.engine = engine
        self.metrics = metrics
        self._lock = threading.Lock()
        self._in_use = 0
        self._peak = 0

    def take_peak(self) -> int:
        """The most connections that were checked out at once since the last call, then reset to
        what is in use now. A gauge read every few seconds cannot see a pool that fills for 3 ms;
        this can, which is what sizing it needs."""
        with self._lock:
            peak = self._peak
            self._peak = self._in_use
            return peak

    @contextmanager
    def connect(self) -> Iterator[Connection]:
        started = time.perf_counter()
        try:
            connection = self.engine.connect()
        except sa_exc.TimeoutError as exc:  # QueuePool limit reached and pool_timeout elapsed
            self.metrics.pool_wait.observe(time.perf_counter() - started)
            self.metrics.pool_timeouts.inc()
            raise DatabaseBusy() from exc
        self.metrics.pool_wait.observe(time.perf_counter() - started)
        with self._lock:
            self._in_use += 1
            self._peak = max(self._peak, self._in_use)
        try:
            yield connection
        finally:
            connection.close()
            with self._lock:
                self._in_use -= 1
