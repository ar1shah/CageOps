"""Loading the active model, verifying it, and switching to a new one without a restart.

The idea in one paragraph: the process holds ONE reference, `current`. A request copies it into a
local variable on its first line and uses only that local. A background poll builds a complete,
verified replacement and then assigns the attribute, which is a single atomic step in CPython, so a
request sees either the whole old model or the whole new one. Requests already running keep the
object they copied and finish on it; the old model is freed when the last of them lets go.
(D-036)

What is deliberately NOT here: a fallback. If the active version cannot be verified, nothing is
served from it, and nothing is served from "some other model file on disk" either.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine

from cageops_api.metrics import ApiMetrics
from cageops_common.inference.bundle import BundleError, LoadedModel, load_bundle
from cageops_common.inference.registry import ActiveVersion, read_active_version

log = logging.getLogger(__name__)

# Readiness tolerates this many poll intervals without a successful database round trip.
STALE_AFTER_POLLS = 3


class ModelUnavailable(Exception):
    """No verified model is loaded; the request cannot be served. Maps to a 503."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ServingModel:
    """What a request holds: the verified model plus the registry facts it was loaded from."""

    model: LoadedModel
    active: ActiveVersion
    loaded_at: datetime

    @property
    def version(self) -> str:
        return self.model.version

    @property
    def features_output_sha256(self) -> str:
        return self.active.features_output_sha256


class ModelManager:
    def __init__(
        self,
        engine: Engine,
        model_dir: Path,
        *,
        poll_interval_s: float,
        retry_s: float,
        metrics: ApiMetrics,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.engine = engine
        self.model_dir = model_dir
        self.poll_interval_s = poll_interval_s
        self.retry_s = retry_s
        self.metrics = metrics
        self._clock = clock
        self._lock = threading.Lock()  # one refresh at a time (the poller, a test, startup)
        self._current: ServingModel | None = None  # the one reference requests copy
        self._failed: dict[str, tuple[float, str]] = {}  # version -> (when, why)
        self._last_error: str | None = "no model has been loaded yet"
        self._last_db_ok: float | None = None
        self._warned_no_active = False
        self.metrics.model_loaded.set(0)

    # -- what requests use ----------------------------------------------------------------

    @property
    def current(self) -> ServingModel | None:
        return self._current

    def require(self) -> ServingModel:
        """The model to serve this request with. Copy the result into a local and use only that."""
        current = self._current
        if current is None:
            raise ModelUnavailable(self._last_error or "no model is loaded")
        return current

    def readiness(self) -> dict[str, Any]:
        now = self._clock()
        current = self._current
        db_fresh = (
            self._last_db_ok is not None
            and now - self._last_db_ok <= STALE_AFTER_POLLS * self.poll_interval_s
        )
        ready = current is not None and db_fresh
        body: dict[str, Any] = {
            "ready": ready,
            "model_version": current.version if current else None,
            "database": "ok" if db_fresh else "unreachable_or_never_checked",
        }
        if not ready:
            body["reason"] = (
                self._last_error
                if current is None
                else "the database has not answered in the last "
                f"{STALE_AFTER_POLLS} polls ({STALE_AFTER_POLLS * self.poll_interval_s:.0f} s)"
            )
        return body

    # -- one poll -------------------------------------------------------------------------

    def refresh(self) -> str:
        """Ask the registry which version is active and load it if it is not the one we serve.

        Returns what happened: `unchanged`, `loaded`, `failed`, `no_active`, `db_error` or
        `backing_off`. Never raises: a poll that dies would end the reload loop."""
        with self._lock:
            return self._refresh()

    def _refresh(self) -> str:
        try:
            with self.engine.connect() as conn:
                active = read_active_version(conn)
        except Exception as exc:  # the database being down must not kill the poller
            # `_last_db_ok` is left alone: readiness goes stale on its own after a few polls
            if self._current is None:
                self._last_error = f"database unreachable: {type(exc).__name__}"
            log.warning("registry poll failed", extra={"error": type(exc).__name__})
            return "db_error"

        self._last_db_ok = self._clock()
        self.metrics.last_poll.set(time.time())

        if active is None:
            self.metrics.active_in_db.set(0)
            if self._current is None:
                self._last_error = "the registry has no active model version"
            elif not self._warned_no_active:
                self._warned_no_active = True
                log.warning(
                    "the registry has no active version; keeping the one being served",
                    extra={"model_version": self._current.version},
                )
            return "no_active"
        self._warned_no_active = False
        self.metrics.active_in_db.set(1)

        if self._current is not None and self._current.version == active.version:
            return "unchanged"

        failed = self._failed.get(active.version)
        if failed is not None and self._clock() - failed[0] < self.retry_s:
            return "backing_off"

        return self._load(active)

    def _load(self, active: ActiveVersion) -> str:
        started = time.perf_counter()
        try:
            loaded = load_bundle(
                self.model_dir / active.version, active.artifacts_sha256, version=active.version
            )
        except Exception as exc:  # BundleError, or an OSError / LightGBM error: same outcome
            reason = str(exc) if isinstance(exc, BundleError) else f"{type(exc).__name__}: {exc}"
            self._failed[active.version] = (self._clock(), reason)
            self.metrics.reloads.labels("failure").inc()
            serving = self._current.version if self._current else None
            if self._current is None:
                self._last_error = f"model {active.version} could not be verified: {reason}"
            log.error(
                "model_reload_failed",
                extra={
                    "model_version": active.version,
                    "reason": reason,
                    "still_serving": serving,
                },
            )
            return "failed"

        elapsed = time.perf_counter() - started
        previous = self._current.version if self._current else None
        # The swap. Everything above built and checked the replacement; this single assignment is
        # what makes it visible, and requests that already copied the old reference finish on it.
        self._current = ServingModel(loaded, active, datetime.now(UTC))
        self._failed.pop(active.version, None)
        self._last_error = None
        self.metrics.reloads.labels("success").inc()
        self.metrics.model_loaded.set(1)
        self.metrics.model_load_seconds.set(elapsed)
        self.metrics.model_info.clear()  # never leave the old version's series at 1
        self.metrics.model_info.labels(active.version, active.features_output_sha256).set(1)
        log.info(
            "model_loaded",
            extra={
                "model_version": active.version,
                "previous_version": previous,
                "features_output_sha256": active.features_output_sha256,
                "data_cutoff": active.data_cutoff.isoformat(),
                "artifacts_sha256": active.artifacts_sha256,
                "load_ms": round(elapsed * 1000, 1),
            },
        )
        return "loaded"
