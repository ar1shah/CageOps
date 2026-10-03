"""Circuit breaker for "the site is refusing automated clients" (D-013).

Like the breaker in a house's fuse box: one fault trips it, power cuts to every room at once
(every worker stops fetching), and a person has to go look at what happened before flipping it
back. It does NOT retry by itself every few seconds, because probing a site that just told us
to go away is exactly what we promised not to do. There is deliberately no expiry.

The state is one Redis key per source: {reason, url, detected_at}.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from redis import Redis


@dataclass(frozen=True)
class BreakerState:
    reason: str
    url: str
    detected_at: str  # ISO timestamp


class CircuitBreaker:
    def __init__(self, redis: Redis, source: str):
        self.source = source
        self.key = f"breaker:{source}"
        self._redis = redis

    def trip(self, reason: str, url: str, now: datetime) -> bool:
        """Open the breaker. Returns True if this call opened it, False if it was already open
        (the first detection is kept, since it says what happened first)."""
        payload = json.dumps({"reason": reason, "url": url, "detected_at": now.isoformat()})
        return bool(self._redis.set(self.key, payload, nx=True))

    def state(self) -> BreakerState | None:
        raw = self._redis.get(self.key)
        if raw is None:
            return None
        return BreakerState(**json.loads(raw))

    def reset(self) -> bool:
        """Close the breaker by hand. Returns False if it wasn't open."""
        return self._redis.delete(self.key) == 1
