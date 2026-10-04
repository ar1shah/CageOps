"""/healthz is a pulse; /metrics shows Redis-side facts. Neither does I/O to answer (D-024)."""

import time
from datetime import date

import httpx
import pytest
from prometheus_client import REGISTRY, CollectorRegistry, generate_latest

from cageops_common.db.session import make_engine
from cageops_worker.ingest.collector import QueueCollector
from cageops_worker.ingest.health import (
    HealthServer,
    Heartbeat,
    WorkerStatus,
    health_response,
)
from cageops_worker.ingest.observe import Observer, probe_redis
from cageops_worker.ingest.runs import enqueue_backfill

SINCE = date(2026, 1, 1)
MAX_AGE = 360.0  # job timeout 300 + margin 60


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def status(clock):
    return WorkerStatus(Heartbeat(clock), MAX_AGE)


@pytest.fixture
def observer(ctx, clock):
    return Observer(ctx.redis, ctx.engine, ctx.settings.ingest_queue, ctx.source.name, clock=clock)


def dead_redis():
    return probe_redis("redis://127.0.0.1:1/0", 0.2)


# -- liveness: the pulse ------------------------------------------------------------------


def test_a_fresh_pulse_is_healthy(status, observer, clock):
    observer.refresh()
    clock.now += 5

    code, body = health_response(status, observer)

    assert code == 200 and body["status"] == "ok"
    assert body["pulse_age_s"] == 5.0 and body["max_pulse_age_s"] == MAX_AGE


def test_a_stale_pulse_is_503(status, observer, clock):
    clock.now += MAX_AGE + 1  # the loop hasn't ticked for longer than a job may run

    code, body = health_response(status, observer)

    assert code == 503 and body["status"] == "stale"


def test_a_beat_makes_it_healthy_again(status, observer, clock):
    clock.now += MAX_AGE + 1
    status.heartbeat.beat()

    assert health_response(status, observer)[0] == 200


def test_pulse_exactly_at_the_limit_is_still_healthy(status, observer, clock):
    clock.now += MAX_AGE

    assert health_response(status, observer)[0] == 200


def test_redis_down_is_still_200_and_the_body_says_so(status, ctx, clock):
    """A Redis blip must not restart every worker at once: a restart doesn't fix Redis."""
    observer = Observer(dead_redis(), ctx.engine, "ingest", "ufcstats", clock=clock)
    observer.refresh()

    code, body = health_response(status, observer)

    assert code == 200
    assert body["dependencies"]["redis"] == "down"
    assert body["queue"] is None and body["dead_letters"] is None  # unknown, not zero


def test_postgres_down_is_still_200(status, ctx, clock):
    dead_db = make_engine("postgresql://u:p@127.0.0.1:1/x", connect_args={"connect_timeout": 1})
    observer = Observer(ctx.redis, dead_db, "ingest", "ufcstats", clock=clock)
    observer.refresh()

    code, body = health_response(status, observer)

    assert code == 200
    assert body["dependencies"] == {"redis": "ok", "postgres": "down", "checked_age_s": 0.0}


def test_an_open_breaker_is_reported_but_is_still_200(status, observer, ctx):
    ctx.fetcher.breaker.trip("js_challenge", "http://ufcstats.com/x", ctx.fetcher._clock())
    observer.refresh()

    code, body = health_response(status, observer)

    assert code == 200
    assert body["breaker"]["reason"] == "js_challenge"


def test_before_the_first_snapshot_everything_is_unknown_not_an_error(status, observer):
    code, body = health_response(status, observer)

    assert code == 200
    assert body["dependencies"] == {
        "redis": "unknown",
        "postgres": "unknown",
        "checked_age_s": None,
    }


def test_the_body_reports_what_the_worker_is_doing(status, observer):
    status.set_state("hold:breaker")

    assert health_response(status, observer)[1]["worker_state"] == "hold:breaker"
    status.set_state("running")


# -- over real HTTP -----------------------------------------------------------------------


@pytest.fixture
def server(status, observer):
    registry = CollectorRegistry()
    registry.register(QueueCollector(observer))
    http = HealthServer("127.0.0.1", 0, status, observer, registry)
    http.start()
    yield http
    http.stop()


def get(server: HealthServer, path: str) -> httpx.Response:
    return httpx.get(f"http://127.0.0.1:{server.port}{path}", timeout=2)


def test_healthz_over_http(server, observer):
    observer.refresh()

    response = get(server, "/healthz")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json()["dependencies"]["redis"] == "ok"


def test_healthz_over_http_goes_503_when_stale(server, clock):
    clock.now += MAX_AGE + 1

    assert get(server, "/healthz").status_code == 503


def test_an_unknown_path_is_404(server):
    assert get(server, "/nope").status_code == 404


def test_the_handler_answers_instantly_even_if_redis_is_down_and_never_looked(status, ctx):
    """No I/O in the request path: a probe with a ~1 s timeout must never wait on a dead Redis."""
    observer = Observer(dead_redis(), ctx.engine, "ingest", "ufcstats")  # never refreshed
    http = HealthServer("127.0.0.1", 0, status, observer, CollectorRegistry())
    http.start()
    try:
        response = httpx.get(f"http://127.0.0.1:{http.port}/healthz", timeout=0.15)
        assert response.status_code == 200
    finally:
        http.stop()


# -- metrics ------------------------------------------------------------------------------


def sample(text: str, prefix: str) -> str | None:
    return next((line for line in text.splitlines() if line.startswith(prefix)), None)


def test_metrics_is_prometheus_text_with_queue_depth_matching_redis(
    server, observer, ctx, site, run_jobs
):
    enqueue_backfill(ctx, SINCE)
    observer.refresh()

    text = get(server, "/metrics").text

    assert sample(text, 'ingest_queue_depth{queue="ingest",state="queued"}') == (
        'ingest_queue_depth{queue="ingest",state="queued"} 1.0'
    )
    assert sample(text, 'ingest_queue_depth{queue="ingest",state="failed"}').endswith(" 0.0")
    assert sample(text, 'scraper_breaker_open{source="ufcstats"}').endswith(" 0.0")


def test_dead_letters_show_up_by_reason(server, observer, ctx, site, run_jobs):
    site.remove("/statistics/events/completed")
    enqueue_backfill(ctx, SINCE)
    run_jobs()
    observer.refresh()

    text = get(server, "/metrics").text

    assert sample(text, 'ingest_dead_letters_current{reason="not_found"}').endswith(" 1.0")
    assert sample(text, 'ingest_queue_depth{queue="ingest",state="failed"}').endswith(" 1.0")


def test_an_open_breaker_is_a_gauge_of_one(server, observer, ctx):
    ctx.fetcher.breaker.trip("js_challenge", "http://ufcstats.com/x", ctx.fetcher._clock())
    observer.refresh()

    text = get(server, "/metrics").text

    assert sample(text, 'scraper_breaker_open{source="ufcstats"}').endswith(" 1.0")


def test_redis_down_leaves_the_gauges_out_instead_of_reporting_zero(ctx, clock):
    observer = Observer(dead_redis(), ctx.engine, "ingest", "ufcstats", clock=clock)
    observer.refresh()
    registry = CollectorRegistry()
    registry.register(QueueCollector(observer))

    text = generate_latest(registry).decode()

    assert "ingest_queue_depth{" not in text
    assert "scraper_breaker_open{" not in text


def test_a_job_run_in_this_process_is_visible_in_the_metrics(ctx, site, run_jobs):
    """The point of SimpleWorker (D-022): a job's counters are in this process's /metrics."""
    labels = {"job_type": "discover_events", "outcome": "success"}
    before = REGISTRY.get_sample_value("ingest_jobs_total", labels) or 0.0
    enqueue_backfill(ctx, SINCE)

    run_jobs()

    # the event-list jobs for page 1 and page 2 both succeed
    assert REGISTRY.get_sample_value("ingest_jobs_total", labels) == before + 2
    assert b"ingest_jobs_total" in generate_latest(REGISTRY)


def test_worker_hold_gauge_follows_the_state(status):
    status.set_state("hold:breaker")
    assert REGISTRY.get_sample_value("ingest_worker_hold", {"reason": "breaker"}) == 1.0

    status.set_state("running")
    assert REGISTRY.get_sample_value("ingest_worker_hold", {"reason": "breaker"}) is None


# -- the background thread ----------------------------------------------------------------


def test_the_background_thread_takes_snapshots_until_stopped(observer):
    assert observer.snapshot().taken_at is None

    observer.start(0.02)
    try:
        deadline = time.monotonic() + 3
        while observer.snapshot().taken_at is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert observer.snapshot().redis_ok is True
    finally:
        observer.stop()
