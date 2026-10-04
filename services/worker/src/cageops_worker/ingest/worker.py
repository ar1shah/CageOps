"""The worker process: one RQ SimpleWorker that holds still while the site is blocking us (D-024).

Why "hold" instead of "exit": a Deployment restarts a container however it exits, so a worker that
quit because the site blocked us would just crash-loop, re-fetching robots.txt each time, and
restarting fixes nothing: only a person can clear the circuit breaker. So the worker stays up,
keeps its pulse going, takes no jobs (the queue stays untouched, not drained into the dead-letter
queue) and checks every few seconds whether the breaker has been reset. Like a cook who, told the
supplier has cut us off, stands at the stove instead of leaving or placing more orders.

Exit codes are for one-shot callers (the CLI, CronJobs, scripts): `--burst` is one-shot, so with the
breaker open it exits 3 and leaves the queue as it found it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from prometheus_client import REGISTRY
from redis.exceptions import RedisError
from rq import SimpleWorker
from sqlalchemy.exc import SQLAlchemyError

from cageops_scraper.errors import RobotsDisallowed, RobotsUnavailable, SourceBlocked
from cageops_worker.ingest.collector import QueueCollector
from cageops_worker.ingest.context import IngestContext, set_context
from cageops_worker.ingest.health import HealthServer, Heartbeat, WorkerStatus
from cageops_worker.ingest.observe import Observer

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_REFUSED = 3  # the site is blocking us / robots.txt says no: nothing was done

WORKER_TTL_S = 30  # RQ wakes an idle worker every WORKER_TTL_S - 15 s, which is the pulse's tick
RESTART_WAIT_S = 5.0


def gate(ctx: IngestContext) -> str | None:
    """May we fetch? None if so, else why not: breaker | robots_disallowed | robots_unavailable.

    Runs the same preflight a scraper start-up always does (breaker, robots.txt, Crawl-delay).
    """
    try:
        ctx.fetcher.preflight()
    except SourceBlocked:
        return "breaker"
    except RobotsDisallowed:
        return "robots_disallowed"
    except RobotsUnavailable:
        return "robots_unavailable"
    except (RedisError, SQLAlchemyError):
        log.exception("preflight could not run (Redis or Postgres trouble); holding")
        return "preflight_error"
    return None


class IngestWorker(SimpleWorker):
    """SimpleWorker (so metrics survive, D-022) plus a pulse and the hold."""

    def __init__(
        self,
        *args: Any,
        ctx: IngestContext,
        status: WorkerStatus,
        hold_poll_s: float,
        exit_when_held: bool = False,
        after_scheduler: Callable[[], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        **kwargs: Any,
    ):
        super().__init__(*args, worker_ttl=WORKER_TTL_S, **kwargs)
        self.ctx = ctx
        self.status = status
        self.hold_poll_s = hold_poll_s
        self.exit_when_held = exit_when_held
        self.held_reason: str | None = None  # set when exit_when_held cut the run short
        self._after_scheduler = after_scheduler
        self._sleep = sleep
        self._cleared = False  # has the gate passed since the last time we were held?

    def _start_scheduler(self, *args: Any, **kwargs: Any) -> None:
        # RQ forks the scheduler here. Our HTTP and observer threads start only afterwards, so the
        # fork never happens in a multi-threaded process (which can deadlock the child).
        super()._start_scheduler(*args, **kwargs)
        if self._after_scheduler is not None:
            self._after_scheduler()

    # The pulse. Stamp first: it must tick even when Redis is down and RQ's own heartbeat raises.
    def heartbeat(self, timeout: int | None = None, pipeline: Any = None) -> None:
        self.status.heartbeat.beat()
        super().heartbeat(timeout, pipeline)

    def dequeue_job_and_maintain_ttl(self, timeout, max_idle_time=None):
        if not self._wait_until_clear():
            return None  # a stop was requested (or exit_when_held) while holding
        self.status.set_state("running")
        return super().dequeue_job_and_maintain_ttl(timeout, max_idle_time)

    def _blocked(self) -> str | None:
        """Why we may not take a job right now, or None. One Redis GET per dequeue once cleared."""
        if self._cleared:
            try:
                if self.ctx.fetcher.breaker.state() is None:
                    return None
            except RedisError:
                return None  # can't tell; not a block. RQ's own dequeue retries a dead Redis.
            self._cleared = False
        reason = gate(self.ctx)
        self._cleared = reason is None
        return reason

    def _wait_until_clear(self) -> bool:
        """Block (taking no jobs, pulse ticking) until we may fetch. False if told to stop."""
        reason = self._blocked()
        if reason is None:
            return True
        log.warning("holding: not taking jobs", extra={"reason": reason})
        self.status.set_state(f"hold:{reason}")
        while True:
            if self.exit_when_held:
                self.held_reason = reason
                return False
            waited = 0.0
            while waited < self.hold_poll_s:
                if self._stop_requested:
                    return False
                self.status.heartbeat.beat()
                step = min(1.0, self.hold_poll_s - waited)
                self._sleep(step)
                waited += step
            reason = self._blocked()
            if reason is None:
                log.warning("hold over: taking jobs again")
                return True
            self.status.set_state(f"hold:{reason}")


def run_worker(
    ctx: IngestContext,
    observer: Observer,
    *,
    burst: bool = False,
    max_jobs: int | None = None,
    port: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Run one worker until it is told to stop (or, with burst/max_jobs, runs out of work).
    Returns the exit code."""
    settings = ctx.settings
    set_context(ctx)
    status = WorkerStatus(Heartbeat(), settings.worker_max_pulse_age_s)
    collector = QueueCollector(observer)
    REGISTRY.register(collector)
    port = settings.worker_metrics_port if port is None else port
    server = HealthServer(settings.worker_http_host, port, status, observer) if port else None
    started = False

    def start_threads() -> None:
        nonlocal started
        if started:  # the loop below may build a second worker; the threads are per process
            return
        started = True
        observer.start(settings.worker_snapshot_interval_s)
        if server is not None:
            server.start()

    try:
        return _work_loop(ctx, status, burst, max_jobs, sleep, start_threads)
    finally:
        status.set_state("stopping")
        if server is not None:
            server.stop()
        observer.stop()
        REGISTRY.unregister(collector)


def _work_loop(
    ctx: IngestContext,
    status: WorkerStatus,
    burst: bool,
    max_jobs: int | None,
    sleep: Callable[[float], None],
    after_scheduler: Callable[[], None],
) -> int:
    settings = ctx.settings
    while True:
        worker = IngestWorker(
            [ctx.queue],
            connection=ctx.redis,
            ctx=ctx,
            status=status,
            hold_poll_s=settings.worker_hold_poll_s,
            exit_when_held=burst,
            after_scheduler=after_scheduler,
            sleep=sleep,
        )
        worker.work(burst=burst, max_jobs=max_jobs, with_scheduler=True)
        if worker.held_reason is not None:
            log.error("refusing to run: %s", worker.held_reason)
            return EXIT_REFUSED
        # work() also returns on a stop request, and after a burst or max_jobs. Only an unplanned
        # return (a Redis timeout, an RQ error) means "build a new worker and carry on".
        if burst or max_jobs is not None or worker._stop_requested:
            return EXIT_OK
        log.error("worker loop ended unexpectedly; starting a new worker")
        status.heartbeat.beat()
        sleep(RESTART_WAIT_S)
