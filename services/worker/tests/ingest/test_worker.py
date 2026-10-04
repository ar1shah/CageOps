"""The worker process: holding while the site blocks us, the pulse, and the start-up sequence."""

import signal
from datetime import date

import pytest
from prometheus_client import REGISTRY
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import text

from cageops_worker.ingest.health import Heartbeat, WorkerStatus, health_response
from cageops_worker.ingest.observe import Observer
from cageops_worker.ingest.runs import enqueue_backfill, enqueue_upcoming
from cageops_worker.ingest.worker import (
    EXIT_OK,
    EXIT_REFUSED,
    IngestWorker,
    gate,
    run_worker,
)

SINCE = date(2026, 1, 1)
TRIP = ("js_challenge", "http://ufcstats.com/statistics/events/completed")


class FakeTime:
    """A clock and a sleep that moves it, so a minute of holding takes no time."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = 0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.now += seconds


@pytest.fixture
def fake_time():
    return FakeTime()


@pytest.fixture
def status(fake_time):
    return WorkerStatus(Heartbeat(fake_time), 360.0)


@pytest.fixture
def observer(ctx):
    return Observer(ctx.redis, ctx.engine, ctx.settings.ingest_queue, ctx.source.name)


def trip(ctx) -> None:
    ctx.fetcher.breaker.trip(*TRIP, ctx.fetcher._clock())


def make_worker(ctx, status, fake_time, **kwargs) -> IngestWorker:
    return IngestWorker(
        [ctx.queue],
        connection=ctx.redis,
        ctx=ctx,
        status=status,
        hold_poll_s=30,
        sleep=fake_time.sleep,
        **kwargs,
    )


@pytest.fixture(autouse=True)
def no_hangs():
    """A worker that never stops would hang the suite; fail the test instead after 30 s."""

    def boom(signum, frame):
        raise TimeoutError("worker test ran longer than 30 s")

    signal.signal(signal.SIGALRM, boom)
    signal.alarm(30)
    yield
    signal.alarm(0)


# -- the gate -----------------------------------------------------------------------------


def test_the_gate_is_open_when_nothing_is_wrong(ctx):
    assert gate(ctx) is None


def test_the_gate_names_an_open_breaker(ctx):
    trip(ctx)

    assert gate(ctx) == "breaker"


def test_the_gate_names_a_robots_ban(ctx, site):
    site.add("/robots.txt", "User-agent: *\nDisallow: /")

    assert gate(ctx) == "robots_disallowed"


def test_the_gate_names_an_unreachable_robots_txt(ctx, site):
    site.add("/robots.txt", "oops", status=500)

    assert gate(ctx) == "robots_unavailable"


# -- holding ------------------------------------------------------------------------------


def test_with_the_breaker_open_the_worker_takes_no_jobs_and_keeps_its_pulse(
    ctx, status, observer, fake_time, run_jobs
):
    enqueue_backfill(ctx, SINCE)
    trip(ctx)
    seen = []

    def sleep(seconds):
        fake_time.sleep(seconds)
        seen.append((ctx.queue.count, status.state, status.heartbeat.age()))
        if len(seen) == 100:  # a person looked at the site and reset the breaker
            ctx.fetcher.breaker.reset()

    worker = make_worker(ctx, status, fake_time)
    worker._sleep = sleep
    worker.work(burst=True)

    held = seen[:99]
    assert len(held) == 99  # a hundred seconds of holding, in one-second slices
    assert all(queued == 1 for queued, _, _ in held)  # nothing was dequeued
    assert all(state == "hold:breaker" for _, state, _ in held)
    assert all(age <= 1.0 for _, _, age in held)  # the pulse kept ticking: /healthz stays green
    assert health_response(status, observer)[0] == 200
    assert ctx.queue.count == 0  # and after the reset the job (and what it queued) drained
    assert status.state == "running"


def test_a_hold_polls_the_breaker_only_every_poll_interval(ctx, status, fake_time):
    trip(ctx)
    checks = []
    real = ctx.fetcher.preflight

    def counting_preflight():
        checks.append(fake_time.now)
        return real()

    ctx.fetcher.preflight = counting_preflight

    def sleep(seconds):
        fake_time.sleep(seconds)
        if fake_time.now >= 1100:
            ctx.fetcher.breaker.reset()

    worker = make_worker(ctx, status, fake_time)
    worker._sleep = sleep
    worker.work(burst=True)

    gaps = [b - a for a, b in zip(checks, checks[1:], strict=False)]
    assert gaps and all(gap == 30 for gap in gaps)  # not a request or a Redis read per second


def test_a_stop_request_ends_a_hold(ctx, status, fake_time):
    trip(ctx)
    worker = make_worker(ctx, status, fake_time)

    def sleep(seconds):
        fake_time.sleep(seconds)
        worker._stop_requested = True  # what SIGTERM does

    worker._sleep = sleep

    assert worker._wait_until_clear() is False


def test_a_redis_error_while_checking_the_breaker_is_not_treated_as_a_block(
    ctx, status, fake_time, monkeypatch
):
    worker = make_worker(ctx, status, fake_time)
    assert worker._blocked() is None  # passes the gate once
    monkeypatch.setattr(
        ctx.fetcher.breaker, "state", lambda: (_ for _ in ()).throw(RedisConnectionError("down"))
    )

    assert worker._blocked() is None  # RQ's own dequeue retries a dead Redis; we don't hold


def test_a_breaker_that_trips_mid_run_stops_further_dequeues(ctx, status, fake_time, dead_letters):
    """The in-flight job fails; the rest of the queue waits instead of draining into the DLQ."""
    enqueue_backfill(ctx, SINCE)
    enqueue_upcoming(ctx)  # two independent jobs on the rail
    worker = make_worker(ctx, status, fake_time, exit_when_held=True)
    original = worker.perform_job
    jobs_run = []

    def perform(job, queue):
        jobs_run.append(job.id)
        if len(jobs_run) == 1:
            trip(ctx)  # the first job's response was a bot challenge
        return original(job, queue)

    worker.perform_job = perform
    worker.work(burst=True)

    assert len(jobs_run) == 1
    assert worker.held_reason == "breaker"
    assert ctx.queue.count == 1  # the other job is still queued, not dead-lettered
    letters = dead_letters()
    assert len(letters) == 1  # only the in-flight job, and it can be replayed after a reset
    assert next(iter(letters.values())).meta["reason"] == "source_blocked"


# -- run_worker: the start-up sequence and exit codes -----------------------------------------


def test_burst_with_the_breaker_open_exits_3_and_leaves_the_queue_alone(ctx, observer, site):
    enqueue_backfill(ctx, SINCE)
    trip(ctx)
    requests_before = len(site.requests)

    code = run_worker(ctx, observer, burst=True, port=0)

    assert code == EXIT_REFUSED
    assert ctx.queue.count == 1
    assert len(site.requests) == requests_before  # nothing was fetched


def test_burst_when_robots_disallows_exits_3_without_fetching_pages(ctx, observer, site):
    enqueue_backfill(ctx, SINCE)
    site.add("/robots.txt", "User-agent: *\nDisallow: /")

    code = run_worker(ctx, observer, burst=True, port=0)

    assert code == EXIT_REFUSED
    assert [r for r in site.requests if "robots" not in r] == []


def test_burst_drains_a_backfill_and_exits_0(ctx, observer, db):
    enqueue_backfill(ctx, SINCE)

    code = run_worker(ctx, observer, burst=True, port=0)

    assert code == EXIT_OK
    assert ctx.queue.count == 0
    with db.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM fights")).scalar_one() > 0


def test_max_jobs_stops_after_n_jobs(ctx, observer):
    enqueue_backfill(ctx, SINCE)

    code = run_worker(ctx, observer, max_jobs=1, port=0)

    assert code == EXIT_OK
    assert ctx.queue.count >= 1  # the discovery job queued more; we stopped before running them


def test_a_retry_with_a_delay_fires_under_the_real_entry_point(ctx, observer, site):
    """Delayed retries need RQ's scheduler (with_scheduler=True); without it this would hang
    until the 30 s alarm. A failed first fetch is retried about a second later and succeeds."""
    ctx.settings.ingest_retry_base_s = 1
    site.flaky["/statistics/events/upcoming"] = 1
    enqueue_upcoming(ctx)
    labels = {"job_type": "discover_events", "outcome": "retry"}
    before = REGISTRY.get_sample_value("ingest_jobs_total", labels) or 0.0

    code = run_worker(ctx, observer, max_jobs=2, port=0)

    assert code == EXIT_OK
    assert REGISTRY.get_sample_value("ingest_jobs_total", labels) == before + 1
    assert site.requests.count("http://ufcstats.com/statistics/events/upcoming") == 2
