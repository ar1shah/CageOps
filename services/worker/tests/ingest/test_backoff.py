import random

import pytest

from cageops_worker.ingest.backoff import backoff_schedule


def test_delays_grow_exponentially_within_the_jitter_band():
    delays = backoff_schedule(5, base_s=30, cap_s=900, rng=random.Random(1))

    nominal = [30, 60, 120, 240, 480]
    assert len(delays) == 5
    for delay, d in zip(delays, nominal, strict=True):
        assert d / 2 <= delay <= d  # equal jitter: between half and all of the nominal delay


def test_the_cap_limits_the_delay():
    delays = backoff_schedule(8, base_s=30, cap_s=100, rng=random.Random(2))

    assert max(delays) <= 100
    assert all(50 <= d <= 100 for d in delays[2:])  # nominal reached the cap from the 3rd on


def test_jitter_actually_varies_between_jobs():
    schedules = {tuple(backoff_schedule(5, 30, 900, random.Random(seed))) for seed in range(20)}

    assert len(schedules) > 15  # 20 jobs do not all retry on the same seconds


def test_the_same_seed_gives_the_same_schedule():
    assert backoff_schedule(5, 30, 900, random.Random(7)) == backoff_schedule(
        5, 30, 900, random.Random(7)
    )


def test_a_zero_base_means_retry_immediately():
    assert backoff_schedule(4, base_s=0, cap_s=900) == [0, 0, 0, 0]


def test_a_tiny_base_never_rounds_down_to_an_immediate_retry():
    assert all(d >= 1 for d in backoff_schedule(5, base_s=1, cap_s=900, rng=random.Random(3)))


@pytest.mark.parametrize("retries", [0, 1, 5])
def test_one_delay_per_retry(retries):
    assert len(backoff_schedule(retries, 30, 900)) == retries
