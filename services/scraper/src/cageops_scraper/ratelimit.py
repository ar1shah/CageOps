"""A rate limiter shared by every worker, kept in Redis.

How it works: a deli counter's "take a number" machine. There is one machine for the whole store
(one Redis key). Each worker pulls a ticket, and the ticket says exactly when it will be served:
the first ticket is for "now", the next for one interval later, and so on. The worker then
sleeps until its time. Nobody polls, nobody elbows, and it doesn't matter how many workers
are in line.

The key holds one number: the earliest time (ms) the NEXT request may go out.

Why it must be atomic. A naive limiter reads the number, checks it, then writes a new one:

    worker A: GET key -> 1000      worker B: GET key -> 1000     (both see "slot 1000 is free")
    worker A: SET key 2000         worker B: SET key 2000        (both fetch at 1000: two requests
                                                                  at once, the limit is broken)

That is check-then-act: like two people booking the last seat because both saw it free. The
fix is to make read + write one indivisible step. Redis runs one script at a time, so a Lua
script that does GET, compute and SET cannot be interleaved with anyone else's.

Time comes from Redis (the TIME command), not from the worker, so containers whose clocks
disagree still agree on the schedule.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from redis import Redis

from cageops_scraper.metrics import RATELIMIT_WAIT_SECONDS

# KEYS[1] = the key, ARGV[1] = interval in ms. Returns {absolute slot (ms), wait (ms)}.
# The key expires a minute after its slot passes, so an idle limiter leaves nothing behind.
_RESERVE = """
local t = redis.call('TIME')
local now = t[1] * 1000 + math.floor(t[2] / 1000)
local nxt = tonumber(redis.call('GET', KEYS[1]) or '0')
local slot = math.max(now, nxt)
local interval = tonumber(ARGV[1])
redis.call('SET', KEYS[1], slot + interval, 'PX', slot + interval - now + 60000)
return {slot, slot - now}
"""

# KEYS[1] = the key, ARGV[1] = how long everyone should back off, in ms.
# Moves the next slot out to at least now + backoff (never earlier). Returns the new next slot.
_PENALIZE = """
local t = redis.call('TIME')
local now = t[1] * 1000 + math.floor(t[2] / 1000)
local nxt = tonumber(redis.call('GET', KEYS[1]) or '0')
local target = now + tonumber(ARGV[1])
if target > nxt then
  redis.call('SET', KEYS[1], target, 'PX', target - now + 60000)
  return target
end
return nxt
"""


@dataclass(frozen=True)
class Reservation:
    slot_ms: int  # absolute time (Redis clock, ms since epoch) this request may go out
    wait_ms: int  # how long to sleep from the moment of reserving


class RateLimiter:
    def __init__(
        self,
        redis: Redis,
        source: str,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.source = source
        self.key = f"ratelimit:{source}"  # one limit per site
        self._redis = redis
        self._reserve = redis.register_script(_RESERVE)
        self._penalize = redis.register_script(_PENALIZE)
        self._sleep = sleep

    def reserve(self, interval_ms: int) -> Reservation:
        """Take the next slot without sleeping. Atomic across every worker."""
        slot, wait = self._reserve(keys=[self.key], args=[interval_ms])
        return Reservation(slot_ms=int(slot), wait_ms=int(wait))

    def acquire(self, interval_ms: int) -> Reservation:
        """Take the next slot and sleep until it arrives. Call right before sending a request."""
        reservation = self.reserve(interval_ms)
        RATELIMIT_WAIT_SECONDS.labels(self.source).observe(reservation.wait_ms / 1000)
        if reservation.wait_ms > 0:
            self._sleep(reservation.wait_ms / 1000)
        return reservation

    def penalize(self, seconds: float) -> int:
        """The site told us to back off (429 + Retry-After): push the next slot out for ALL
        workers. Returns the new next-slot time (ms)."""
        return int(self._penalize(keys=[self.key], args=[int(seconds * 1000)]))
