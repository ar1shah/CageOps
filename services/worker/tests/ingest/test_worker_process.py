"""The real worker process (python -m cageops_worker.ingest worker), end to end.

Each test runs the actual command-line entry point as a subprocess against the test Postgres and
Redis, with the stand-in site (scripts/standin_site.py) playing ufcstats on a local port. RQ
installs signal handlers, which only work on a main thread, so this can't be done in-process.
"""

import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import text
from standin_site import make_server

from cageops_scraper.breaker import CircuitBreaker

SOURCE = "ufcstats_replay"
SINCE = "2026-04-18"
UA = "CageOps/0.1 (+https://github.com/ar1shah/cageops; test@cageops.dev)"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_until(check, what: str, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f"timed out after {timeout:.0f}s waiting for: {what}")


class Stack:
    """The pieces around the worker: the stand-in site, the env, and helpers to drive the CLI."""

    def __init__(self, tmp_path, db, redis_client, interval_ms: int = 50):
        self.db, self.redis = db, redis_client
        server, self.site = make_server(0)
        self._server = server
        threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        ).start()
        self.site_url = f"http://127.0.0.1:{server.server_address[1]}"
        kwargs = redis_client.connection_pool.connection_kwargs
        self.metrics_port = free_port()
        # A queue of its own, so a worker left over from an interrupted run (they outlive a killed
        # pytest) can never steal this test's jobs.
        self.queue = f"ingest-test-{uuid.uuid4().hex[:8]}"
        self.cwd = tmp_path  # not the repo root: keeps a developer's .env out of the picture
        self.env = os.environ | {
            "DATABASE_URL": db.url.render_as_string(hide_password=False),
            "REDIS_URL": f"redis://{kwargs['host']}:{kwargs['port']}/{kwargs['db']}",
            "SCRAPER_USER_AGENT": UA,
            "SCRAPER_SOURCE": SOURCE,
            "UFCSTATS_REPLAY_BASE_URL": self.site_url,
            "SCRAPER_MIN_INTERVAL_MS": str(interval_ms),
            "INGEST_QUEUE": self.queue,
            "INGEST_RETRY_BASE_S": "0",
            "WORKER_HOLD_POLL_S": "0.3",
            "WORKER_SNAPSHOT_INTERVAL_S": "0.2",
            "WORKER_METRICS_PORT": str(self.metrics_port),
            "PYTHONWARNINGS": "always::DeprecationWarning",
        }
        self.procs: list[subprocess.Popen] = []

    def cli(self, *argv: str, timeout: float = 60) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "cageops_worker.ingest", *argv],
            env=self.env, cwd=self.cwd, capture_output=True, text=True, timeout=timeout,
        )  # fmt: skip

    def start_worker(self, *argv: str) -> subprocess.Popen:
        self.stderr_path = self.cwd / f"worker-{len(self.procs)}.log"
        proc = subprocess.Popen(
            [sys.executable, "-m", "cageops_worker.ingest", "worker", *argv],
            env=self.env, cwd=self.cwd, stdout=subprocess.DEVNULL,
            stderr=self.stderr_path.open("w"),
            start_new_session=True,  # its own process group, so close() also reaps RQ's scheduler
        )  # fmt: skip
        self.procs.append(proc)
        return proc

    def worker_log(self) -> str:
        return self.stderr_path.read_text()

    def healthz(self) -> httpx.Response | None:
        try:
            return httpx.get(f"http://127.0.0.1:{self.metrics_port}/healthz", timeout=2)
        except httpx.HTTPError:
            return None

    def metrics(self) -> str:
        return httpx.get(f"http://127.0.0.1:{self.metrics_port}/metrics", timeout=2).text

    def wait_for_state(self, state: str):
        def check():
            response = self.healthz()
            return response is not None and response.json()["worker_state"] == state

        wait_until(check, f"worker state {state!r}")

    def trip_breaker(self) -> None:
        CircuitBreaker(self.redis, SOURCE).trip(
            "js_challenge", f"{self.site_url}/x", datetime.now(UTC)
        )

    def queued(self) -> int:
        return self.redis.llen(f"rq:queue:{self.queue}")

    def count(self, table: str) -> int:
        with self.db.connect() as conn:
            return conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()

    def requests(self) -> int:
        return self.site.requests

    def close(self) -> None:
        for proc in self.procs:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)  # the worker and any scheduler it forked
            proc.wait(timeout=10)
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def stack(tmp_path, db, redis_client):
    s = Stack(tmp_path, db, redis_client)
    yield s
    s.close()


def stop(proc: subprocess.Popen, sig=signal.SIGTERM) -> int:
    proc.send_signal(sig)
    return proc.wait(timeout=30)


# -- the headline: hold while blocked, resume after a reset --------------------------------------


def test_a_blocked_worker_stays_up_takes_no_jobs_and_resumes_after_a_reset(stack):
    assert stack.cli("backfill", "--since", SINCE).returncode == 0
    stack.trip_breaker()
    queued_before = stack.queued()
    assert queued_before == 1

    proc = stack.start_worker()
    stack.wait_for_state("hold:breaker")
    time.sleep(1.5)  # five poll intervals

    assert proc.poll() is None  # still running: no exit, so nothing for a Deployment to restart
    assert stack.healthz().status_code == 200  # and still healthy
    assert stack.queued() == queued_before  # took no jobs
    assert stack.requests() == 0  # sent nothing to the site
    assert 'scraper_breaker_open{source="ufcstats_replay"} 1.0' in stack.metrics()
    assert 'ingest_worker_hold{reason="breaker"} 1.0' in stack.metrics()

    reset = stack.cli("breaker", "reset", "--yes")
    assert reset.returncode == 0

    wait_until(lambda: stack.count("fights") > 0 and stack.queued() == 0, "the queue to drain")
    assert stack.healthz().json()["worker_state"] == "running"
    assert 'scraper_breaker_open{source="ufcstats_replay"} 0.0' in stack.metrics()

    assert stop(proc) == 0


def test_a_breaker_that_trips_mid_run_dead_letters_only_the_job_in_flight(stack):
    stack.cli("backfill", "--since", SINCE)
    stack.cli("scrape-upcoming")  # a second discovery job, so something is left on the rail
    assert stack.queued() == 2
    httpx.post(f"{stack.site_url}/__mode/challenge")  # the site starts serving the bot challenge

    proc = stack.start_worker()
    stack.wait_for_state("hold:breaker")

    assert stack.queued() == 1  # the other job waits; it was not run into the dead-letter queue
    letters = json.loads(stack.cli("dlq", "list", "--json").stdout)
    assert [letter["reason"] for letter in letters] == ["source_blocked"]
    assert proc.poll() is None and stack.healthz().status_code == 200

    # a person checks the site, it's fine again: reset, replay, and everything completes
    httpx.post(f"{stack.site_url}/__mode/ok")
    assert stack.cli("breaker", "reset", "--yes").returncode == 0
    wait_until(lambda: stack.queued() == 0 and stack.count("events") > 0, "the queue to drain")
    assert stack.cli("dlq", "replay", "--all", "--reason", "source_blocked").returncode == 0
    both_discoveries_done = 'ingest_jobs_total{job_type="discover_events",outcome="success"} 2.0'
    wait_until(lambda: both_discoveries_done in stack.metrics(), "the replayed job to succeed")
    reasons = {letter["reason"] for letter in json.loads(stack.cli("dlq", "list", "--json").stdout)}

    assert "source_blocked" not in reasons
    assert stop(proc) == 0


def test_burst_with_the_breaker_open_exits_3_and_leaves_the_queue_alone(stack):
    stack.cli("backfill", "--since", SINCE)
    stack.trip_breaker()

    proc = stack.start_worker("--burst", "--metrics-port", "0")

    assert proc.wait(timeout=30) == 3
    assert stack.queued() == 1 and stack.requests() == 0


def test_burst_drains_the_queue_and_exits_0(stack):
    stack.cli("backfill", "--since", SINCE)

    proc = stack.start_worker("--burst", "--metrics-port", "0")

    assert proc.wait(timeout=60) == 0
    assert stack.queued() == 0 and stack.count("fights") > 0


def test_sigterm_finishes_the_current_job_then_exits_cleanly(tmp_path, db, redis_client):
    stack = Stack(tmp_path, db, redis_client, interval_ms=800)  # slow enough to catch mid-run
    try:
        stack.cli("backfill", "--since", SINCE)
        proc = stack.start_worker()
        wait_until(lambda: stack.count("events") > 0, "the first event to be stored")

        code = stop(proc)

        assert code == 0
        assert stack.queued() > 0  # it stopped before draining the queue
        started = redis_client.zcard(f"rq:wip:{stack.queue}")
        assert started == 0  # no job left half-run (RQ would later call it abandoned)
        assert "worker_lost" not in stack.cli("dlq", "list", "--json").stdout
    finally:
        stack.close()
