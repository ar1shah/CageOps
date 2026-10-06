"""The rate limiter is tested against real Redis: its correctness is "Redis runs the script as
one indivisible step", which a mock can't prove."""

from concurrent.futures import ThreadPoolExecutor

from cageops_scraper.ratelimit import RateLimiter

INTERVAL = 1000


def test_first_request_goes_immediately_and_the_next_waits_one_interval(redis_client):
    limiter = RateLimiter(redis_client, "ufcstats")

    first = limiter.reserve(INTERVAL)
    second = limiter.reserve(INTERVAL)

    assert first.wait_ms == 0
    assert second.slot_ms == first.slot_ms + INTERVAL
    assert 0 < second.wait_ms <= INTERVAL


def test_eight_threads_at_once_never_get_the_same_slot_or_slots_closer_than_the_interval(
    redis_client,
):
    """Absolute slots come from Redis's own clock inside the script, so this can't flake on
    client clock noise: any two slots must differ by at least the interval, by construction."""
    limiter = RateLimiter(redis_client, "ufcstats")

    def grab(_):
        return [limiter.reserve(INTERVAL).slot_ms for _ in range(5)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        slots = sorted(slot for batch in pool.map(grab, range(8)) for slot in batch)

    assert len(slots) == 40
    assert len(set(slots)) == 40
    assert all(b - a >= INTERVAL for a, b in zip(slots, slots[1:], strict=False))


def test_the_naive_read_then_write_limiter_double_books_a_slot(redis_client):
    """The race the Lua script prevents, replayed in a fixed order so it always happens.
    Two workers each read the key, both see the same free slot, then both write."""
    key = "naive:ratelimit"
    now = 1_000_000
    redis_client.set(key, now)

    def read_slot() -> int:  # step 1: GET and decide
        return max(now, int(redis_client.get(key)))

    def write_next(slot: int) -> None:  # step 2: SET the next slot
        redis_client.set(key, slot + INTERVAL)

    slot_a = read_slot()
    slot_b = read_slot()  # worker B reads before worker A has written
    write_next(slot_a)
    write_next(slot_b)

    assert slot_a == slot_b  # both would fetch at the same instant


def test_every_site_has_its_own_limit(redis_client):
    a = RateLimiter(redis_client, "ufcstats")
    b = RateLimiter(redis_client, "ufcstats_replay")

    a.reserve(INTERVAL)

    assert b.reserve(INTERVAL).wait_ms == 0


def test_acquire_sleeps_for_the_wait(redis_client):
    sleeps: list[float] = []
    limiter = RateLimiter(redis_client, "ufcstats", sleep=sleeps.append)

    limiter.acquire(INTERVAL)
    limiter.acquire(INTERVAL)

    assert len(sleeps) == 1  # the first needed no wait
    assert 0 < sleeps[0] <= 1.0


def test_penalize_pushes_the_next_slot_out_for_everyone(redis_client):
    limiter = RateLimiter(redis_client, "ufcstats")
    other_worker = RateLimiter(redis_client, "ufcstats")

    limiter.penalize(30)
    after = other_worker.reserve(INTERVAL)

    assert 29_000 < after.wait_ms <= 30_000


def test_penalize_never_pulls_the_next_slot_earlier(redis_client):
    limiter = RateLimiter(redis_client, "ufcstats")
    limiter.penalize(60)

    limiter.penalize(1)

    assert limiter.reserve(INTERVAL).wait_ms > 58_000


def test_key_expires_so_an_idle_limiter_leaves_nothing_behind(redis_client):
    limiter = RateLimiter(redis_client, "ufcstats")

    limiter.reserve(INTERVAL)

    ttl_ms = redis_client.pttl(limiter.key)
    assert 0 < ttl_ms <= INTERVAL + 60_000
