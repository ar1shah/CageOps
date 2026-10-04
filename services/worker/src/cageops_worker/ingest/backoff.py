"""Retry delays: exponential backoff with jitter.

When a call fails because the other side is busy, wait, then try again, waiting longer each time:
30 s, 60 s, 120 s, ... (that is the exponential part). The jitter randomizes each wait so that
several workers that failed at the same moment don't all retry at the same moment and collide
again. We use "equal jitter": the n-th delay is a random value between half of the nominal delay
and all of it, so it still grows but never collapses to near zero.
"""

from __future__ import annotations

import random


def backoff_schedule(
    retries: int, base_s: int, cap_s: int, rng: random.Random | None = None
) -> list[int]:
    """Seconds to wait before each retry, in order. RQ takes this list as Retry(interval=...)."""
    rng = rng or random.Random()
    delays = []
    for n in range(retries):
        nominal = min(cap_s, base_s * 2**n)
        if nominal <= 0:
            delays.append(0)  # retry immediately
        else:
            delays.append(max(1, round(rng.uniform(nominal / 2, nominal))))
    return delays
