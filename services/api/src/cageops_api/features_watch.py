"""What the stored features were built from, as of the last poll.

The cache key needs two facts about the feature store: WHICH build it is (a rebuild changes the
numbers), and whether the data the build read has changed since (a result ingested, a bio filled
in, a stat corrected). The second is the gap between "a new fight completed" and "someone ran the
rebuild": without it a cached `features_current: true` would go stale the moment a fight completes.

`features_input_fingerprint` is the hash the trainer already uses for this (D-033). It reads the
completed fights, their bios and stats, so it is polled in the background (about 50 ms of
database time, 2026-10-15, BENCHMARKS.md) rather than computed per request; the cost is that
a change is noticed up to one poll late.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from sqlalchemy import Engine, text

from cageops_api.metrics import ApiMetrics
from cageops_common.db.fingerprint import features_input_fingerprint

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FeaturesState:
    build_id: int
    inputs_sha12: str  # the first 12 hex digits of the live input fingerprint
    matches_build: bool  # the live fingerprint equals the one the build stored


class FeaturesWatch:
    def __init__(self, engine: Engine, metrics: ApiMetrics) -> None:
        self.engine = engine
        self.metrics = metrics
        self._lock = threading.Lock()
        self._state: FeaturesState | None = None

    @property
    def state(self) -> FeaturesState | None:
        """The last successful poll's answer, or None before there is one (or when the database
        has never had a features build). One attribute read: safe from any thread."""
        return self._state

    def refresh(self) -> FeaturesState | None:
        """One poll. Never raises: on a database error the previous state stands."""
        with self._lock:
            try:
                with self.engine.connect() as conn:
                    build = conn.execute(
                        text(
                            "SELECT id, report->>'inputs_sha256' AS inputs_sha256 FROM load_runs"
                            " WHERE source = 'features' ORDER BY id DESC LIMIT 1"
                        )
                    ).one_or_none()
                    if build is None:
                        self._state = None
                        return None
                    live = features_input_fingerprint(conn)
            except Exception as exc:  # the poller must survive a database blip
                log.warning("features poll failed", extra={"error": type(exc).__name__})
                return self._state
            state = FeaturesState(
                build_id=int(build.id),
                inputs_sha12=live[:12],
                matches_build=build.inputs_sha256 == live,
            )
            self._state = state
            self.metrics.features_build_id.set(state.build_id)
            self.metrics.features_inputs_match.set(1 if state.matches_build else 0)
            return state
