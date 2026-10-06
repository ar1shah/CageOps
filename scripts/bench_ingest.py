"""Benchmark the ingestion pipeline against the local replay server (docs/BENCHMARKS.md, D-026).

    uv run python scripts/bench_ingest.py run --workers 4 --interval-ms 100 --latency-ms 250
    uv run python scripts/bench_ingest.py matrix            # the whole planned set, ~1 hour

Every number it produces is a REPLAY number: a synthetic year of events served by
scripts/standin_site.py on this machine at a latency we chose. It says nothing about the real site.

One run: reset the benchmark database and Redis, start the replay server and W long-running
workers, queue a 12-month backfill, wait until the queue is idle, collect the numbers, stop
everything, append a JSON line to data/bench/results.jsonl.

Workers are long-running, never `--burst`: a burst worker quits as soon as the queue is empty, and
at the start the queue holds a single discovery job, so 3 of 4 workers would quit at once and the
"4 worker" run would silently be a 1-worker run.

Safety: workers are always pointed at the `ufcstats_replay` source on a loopback address, a relaxed
rate limit (under 1000 ms) is refused for any other source, and the database and Redis it resets
must be the benchmark ones (`cageops_bench`, Redis database 2), never your dev data.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import signal
import socket
import statistics
import subprocess
import sys
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from prometheus_client.parser import text_string_to_metric_families
from replay_corpus import Corpus
from sqlalchemy import make_url, text

from cageops_common.db.session import make_engine

ROOT = Path(__file__).parents[1]
RESULTS = ROOT / "data" / "bench" / "results.jsonl"
LOGS = ROOT / "data" / "bench" / "logs"
BENCH_DB = "cageops_bench"
BENCH_REDIS_DB = 2
REPLAY_SOURCE = "ufcstats_replay"
REAL_FLOOR_MS = 1000
CORPUS_END = date(2026, 9, 26)  # newest synthetic event: old enough that every page is "final"
BACKFILL_SINCE = date(2025, 9, 26)  # 12 months before CORPUS_END
EVENTS = 52
DATA_TABLES = "fight_round_stats, fight_totals, fights, fighters, events"
BENCH_UA = "CageOps-bench/0.1 (+https://github.com/ar1shah/cageops; bench@cageops.dev)"
MODES = ("cold", "warm", "rerun")


class BenchError(RuntimeError):
    pass


# -- configuration and the safety guards -------------------------------------------------------


@dataclass(frozen=True)
class BenchConfig:
    workers: int
    interval_ms: int
    latency_ms: float
    mode: str = "cold"  # cold: nothing cached | warm: pages cached, tables empty | rerun: both full
    events: int = EVENTS
    jitter: float = 0.2
    seed: int = 1
    source: str = REPLAY_SOURCE  # fixed by the harness; a field only so a test can try to break it
    replay_host: str = "127.0.0.1"
    # Fixed, not random: the page cache is keyed by URL, and a warm run only finds the pages a
    # cold run stored if the server has the same address both times.
    server_port: int = 18099
    database_url: str = ""
    redis_url: str = ""
    label: str = ""

    def validate(self) -> None:
        if self.mode not in MODES:
            raise BenchError(f"mode must be one of {MODES}, not {self.mode!r}")
        if self.workers < 1:
            raise BenchError("need at least one worker")
        if self.source != REPLAY_SOURCE and self.interval_ms < REAL_FLOOR_MS:
            raise BenchError(
                f"a rate-limit interval under {REAL_FLOOR_MS} ms is only allowed for the "
                f"{REPLAY_SOURCE!r} source, not {self.source!r} (the real site's floor)"
            )
        if self.source != REPLAY_SOURCE:
            raise BenchError(f"benchmarks only run against {REPLAY_SOURCE!r}, not {self.source!r}")
        if self.replay_host not in ("127.0.0.1", "localhost", "::1"):
            raise BenchError(f"the replay server must be on this machine, not {self.replay_host!r}")
        db_name = make_url(self.database_url).database
        if db_name != BENCH_DB:
            raise BenchError(
                f"refusing to reset database {db_name!r}: benchmarks only use {BENCH_DB!r}"
            )
        redis_db = int(urlsplit(self.redis_url).path.lstrip("/") or 0)
        if redis_db != BENCH_REDIS_DB:
            raise BenchError(
                f"refusing to flush Redis database {redis_db}: benchmarks only use "
                f"database {BENCH_REDIS_DB}"
            )


def bench_urls(env: dict[str, str] | None = None) -> tuple[str, str]:
    """(database url, redis url) for the benchmark: your DATABASE_URL / REDIS_URL with the database
    swapped for `cageops_bench` and Redis database 2. `env` is for tests; normally it reads .env."""
    if env is None:
        from cageops_common.config import get_settings

        settings = get_settings()
        env = {"DATABASE_URL": settings.database_url, "REDIS_URL": settings.redis_url} | dict(
            os.environ
        )
    database = make_url(env.get("BENCH_DATABASE_URL") or env["DATABASE_URL"]).set(database=BENCH_DB)
    redis = urlsplit(env.get("BENCH_REDIS_URL") or env.get("REDIS_URL", "redis://localhost:6379/0"))
    return (
        database.render_as_string(hide_password=False),
        redis._replace(path=f"/{BENCH_REDIS_DB}").geturl(),
    )


def worker_env(
    config: BenchConfig, base_url: str, metrics_port: int, base: dict[str, str] | None = None
) -> dict[str, str]:
    """The environment of one worker process. Always the replay source, whatever else is set."""
    config.validate()
    env = dict(os.environ if base is None else base)
    user_agent = env.get("SCRAPER_USER_AGENT", "")
    if not user_agent or "you@example.com" in user_agent:
        user_agent = BENCH_UA  # only ever sent to the replay server on this machine
    env.update(
        DATABASE_URL=config.database_url,
        REDIS_URL=config.redis_url,
        SCRAPER_USER_AGENT=user_agent,
        SCRAPER_SOURCE=REPLAY_SOURCE,
        UFCSTATS_REPLAY_BASE_URL=base_url,
        SCRAPER_MIN_INTERVAL_MS=str(config.interval_ms),
        WORKER_METRICS_PORT=str(metrics_port),
        WORKER_HTTP_HOST="127.0.0.1",
        WORKER_SNAPSHOT_INTERVAL_S="0.5",
    )
    return env


# -- reading the workers' metrics ------------------------------------------------------------


def parse_metrics(text_body: str) -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    """{(sample name, sorted labels): value} from Prometheus text."""
    samples: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
    for family in text_string_to_metric_families(text_body):
        for sample in family.samples:
            samples[(sample.name, tuple(sorted(sample.labels.items())))] = sample.value
    return samples


def merge(per_worker: Iterable[dict]) -> dict:
    """Add up the same sample across workers (counters and histogram buckets are per process)."""
    total: dict = {}
    for samples in per_worker:
        for key, value in samples.items():
            total[key] = total.get(key, 0.0) + value
    return total


def sum_of(samples: dict, name: str, **labels: str) -> float:
    return sum(
        value
        for (sample, pairs), value in samples.items()
        if sample == name and all(dict(pairs).get(k) == v for k, v in labels.items())
    )


def histogram_quantile(samples: dict, name: str, q: float) -> float | None:
    """The q-quantile of a histogram (seconds), linearly interpolated inside its bucket, like
    Prometheus' histogram_quantile. Buckets are summed over every other label."""
    buckets: dict[float, float] = {}
    for (sample, pairs), value in samples.items():
        if sample == f"{name}_bucket":
            le = float(dict(pairs)["le"].replace("+Inf", "inf"))
            buckets[le] = buckets.get(le, 0.0) + value
    if not buckets or max(buckets.values()) == 0:
        return None
    ordered = sorted(buckets.items())
    rank = q * ordered[-1][1]
    lower, below = 0.0, 0.0
    for upper, cumulative in ordered:
        if cumulative >= rank:
            if upper == float("inf"):
                return lower
            span = cumulative - below
            return upper if span == 0 else lower + (upper - lower) * (rank - below) / span
        lower, below = upper, cumulative
    return None


def summarize_workers(per_worker: list[dict], workers: int, wall_s: float) -> dict:
    total = merge(per_worker)
    fetch_count = sum_of(total, "scraper_fetch_seconds_count")
    wait_sum = sum_of(total, "scraper_ratelimit_wait_seconds_sum")
    job_seconds = sum_of(total, "ingest_job_seconds_sum")
    jobs = {
        outcome: sum_of(total, "ingest_jobs_total", outcome=outcome)
        for outcome in ("success", "retry", "dead_letter")
    }
    return {
        "jobs": jobs,
        "fetches": fetch_count,
        "fetch_p50_s": histogram_quantile(total, "scraper_fetch_seconds", 0.5),
        "fetch_p95_s": histogram_quantile(total, "scraper_fetch_seconds", 0.95),
        "ratelimit_wait_s": wait_sum,
        # of all the time the workers existed, how much was spent waiting for a limiter slot, and
        # how much running jobs (the rest is idle: nothing in the queue)
        "ratelimit_wait_share": wait_sum / (workers * wall_s) if wall_s else None,
        "job_time_share": job_seconds / (workers * wall_s) if wall_s else None,
        "cache_hits": sum_of(total, "scraper_cache_total", result="hit"),
        "cache_misses": sum_of(total, "scraper_cache_total", result="miss"),
    }


def rows_by_action(counts: dict[str, int]) -> dict:
    """{'fights:inserted': 4, ...} -> totals per action plus the same split by table."""
    totals = {"inserted": 0, "updated": 0, "unchanged": 0}
    by_table: dict[str, dict[str, int]] = {}
    for key, value in counts.items():
        table, action = key.split(":", 1)
        by_table.setdefault(table, {})[action] = value
        if table != "fights_scheduled":  # scheduled bouts aren't part of a completed backfill
            totals[action] += value
    return {**totals, "by_table": by_table}


def is_idle(depth: dict[str, int] | None) -> bool:
    """Nothing queued, scheduled for later, or running. An unreadable queue is never idle."""
    return depth is not None and not any(
        depth.get(k, 0) for k in ("queued", "scheduled", "started")
    )


def check_validity(config: BenchConfig, result: dict, corpus: Corpus) -> list[str]:
    """Reasons this run's numbers can't be trusted (empty list: fine)."""
    problems = []
    n = config.events
    if result["dead_letters"]:
        problems.append(f"{result['dead_letters']} dead letters")
    for table, expected in (("events", n), ("fights", 4 * n), ("fighters", 8 * n)):
        if result["table_counts"][table] != expected:
            problems.append(f"{table}: {result['table_counts'][table]} rows, expected {expected}")
    if config.mode == "cold" and result["server_requests"] != corpus.expected_requests():
        problems.append(
            f"server saw {result['server_requests']} requests, "
            f"expected {corpus.expected_requests()}"
        )
    if config.mode in ("warm", "rerun") and result["server_requests"] != 0:
        problems.append(f"{result['server_requests']} requests on a run that should use the cache")
    if config.mode == "rerun" and (result["rows"]["inserted"] or result["rows"]["updated"]):
        problems.append("a rerun wrote rows")
    if config.mode in ("cold", "warm") and result["rows"]["inserted"] == 0:
        problems.append("nothing was inserted")
    return problems


# -- one run -------------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(check, what: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.1)
    raise BenchError(f"timed out after {timeout:.0f}s waiting for {what}")


def ensure_bench_database(config: BenchConfig) -> None:
    """Create the benchmark database if it doesn't exist, and bring it to the latest schema."""
    from alembic import command
    from alembic.config import Config

    url = make_url(config.database_url)
    admin = make_engine(url.set(database="postgres").render_as_string(hide_password=False))
    try:
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": BENCH_DB}
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{BENCH_DB}"'))
    finally:
        admin.dispose()
    engine = make_engine(config.database_url)
    try:
        with engine.begin() as conn:
            cfg = Config(str(ROOT / "packages" / "cageops_common" / "alembic.ini"))
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")
    finally:
        engine.dispose()


def hardware() -> dict:
    cpu = next(
        (
            line.split(":", 1)[1].strip()
            for line in Path("/proc/cpuinfo").read_text().splitlines()
            if line.startswith("model name")
        ),
        platform.processor(),
    )
    mem_kb = next(
        int(line.split()[1])
        for line in Path("/proc/meminfo").read_text().splitlines()
        if line.startswith("MemTotal")
    )
    return {
        "cpu": cpu,
        "logical_cpus": os.cpu_count(),
        "ram_gb": round(mem_kb / 1024 / 1024, 1),
        "os": platform.release(),
        "python": platform.python_version(),
    }


def git_state() -> dict:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
        ).stdout.strip()

    return {"commit": git("rev-parse", "--short", "HEAD"), "dirty": bool(git("status", "--short"))}


class Run:
    """One benchmark run. Use as a context manager so processes always get cleaned up."""

    def __init__(self, config: BenchConfig):
        config.validate()
        self.config = config
        self.corpus = Corpus(config.events, end=CORPUS_END)
        self.procs: list[subprocess.Popen] = []
        self.server_port = config.server_port
        self.base_url = f"http://{config.replay_host}:{self.server_port}"
        self.worker_ports = [free_port() for _ in range(config.workers)]
        LOGS.mkdir(parents=True, exist_ok=True)
        self.stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")

    def __enter__(self) -> Run:
        return self

    def __exit__(self, *exc) -> None:
        self.stop_all()

    # -- processes ---------------------------------------------------------------------

    def start_server(self) -> None:
        c = self.config
        log = (LOGS / f"{self.stamp}-server.log").open("w")
        self.procs.append(
            subprocess.Popen(
                [
                    sys.executable, str(ROOT / "scripts" / "standin_site.py"),
                    "--port", str(self.server_port),
                    "--synthetic-events", str(c.events),
                    "--corpus-end", CORPUS_END.isoformat(),
                    "--latency-ms", str(c.latency_ms),
                    "--jitter", str(c.jitter),
                    "--seed", str(c.seed),
                    "--quiet",
                ],
                stderr=log, stdout=subprocess.DEVNULL, start_new_session=True,
            )
        )  # fmt: skip
        wait_for(lambda: self.server_stats() is not None, "the replay server", 20)

    def server_stats(self) -> dict | None:
        try:
            return httpx.get(f"{self.base_url}/__stats", timeout=2).json()
        except (httpx.HTTPError, ValueError):
            return None

    def start_workers(self) -> None:
        for i, port in enumerate(self.worker_ports):
            log = (LOGS / f"{self.stamp}-worker{i}.log").open("w")
            self.procs.append(
                subprocess.Popen(
                    [
                        sys.executable, "-m", "cageops_worker.ingest", "worker",
                        "--metrics-port", str(port),
                    ],
                    env=worker_env(self.config, self.base_url, port),
                    stderr=log, stdout=subprocess.DEVNULL, start_new_session=True,
                )
            )  # fmt: skip

        def all_running() -> bool:
            for port in self.worker_ports:
                try:
                    body = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=2).json()
                except (httpx.HTTPError, ValueError):
                    return False
                if body.get("worker_state") != "running":
                    return False
            return True

        wait_for(all_running, "every worker to report running", 60)

    def worker_metrics(self) -> list[dict]:
        return [
            parse_metrics(httpx.get(f"http://127.0.0.1:{port}/metrics", timeout=5).text)
            for port in self.worker_ports
        ]

    def stop_all(self) -> None:
        for proc in self.procs:  # workers first (SIGTERM = finish the current job), then the server
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        for proc in self.procs:
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=20)
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)  # also reaps RQ's scheduler process
        self.procs.clear()

    # -- state ------------------------------------------------------------------------

    def reset(self, redis_client, engine) -> None:
        mode = self.config.mode
        redis_client.flushdb()
        if mode in ("cold", "warm"):
            with engine.begin() as conn:
                conn.execute(text(f"TRUNCATE {DATA_TABLES} RESTART IDENTITY CASCADE"))
        if mode == "cold":
            with engine.begin() as conn:
                conn.execute(text("DELETE FROM raw_pages WHERE source = :s"), {"s": REPLAY_SOURCE})

    def table_counts(self, engine) -> dict[str, int]:
        with engine.connect() as conn:
            return {
                t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one()
                for t in ("events", "fights", "fighters", "fight_totals", "fight_round_stats")
            }

    def execute(self) -> dict:
        import redis as redis_lib

        from cageops_worker.ingest.observe import Observer
        from cageops_worker.ingest.runs import start_backfill
        from cageops_worker.ingest.upsert import read_counts

        c = self.config
        env = worker_env(c, self.base_url, 0)
        os.environ.update(env)  # the harness process talks to the same Redis and database
        for cache in _settings_caches():
            cache.cache_clear()
        from cageops_worker.ingest.context import build_context

        engine = make_engine(c.database_url)
        redis_client = redis_lib.Redis.from_url(c.redis_url)
        try:
            self.reset(redis_client, engine)
            if c.mode == "rerun" and self.table_counts(engine)["fights"] == 0:
                raise BenchError("a rerun needs a populated database; run a cold run first")
            self.start_server()
            self.start_workers()
            ctx = build_context()
            observer = Observer(redis_client, engine, ctx.settings.ingest_queue, REPLAY_SOURCE)

            t0 = time.perf_counter()
            started = start_backfill(ctx, BACKFILL_SINCE)
            idle_since = None
            while True:
                time.sleep(0.2)
                snapshot = observer.refresh()
                if is_idle(snapshot.queue_depth):
                    idle_since = idle_since or time.perf_counter()
                    if time.perf_counter() - idle_since >= 0.5:  # two looks, half a second apart
                        break
                else:
                    idle_since = None
                if time.perf_counter() - t0 > 3 * 3600:
                    raise BenchError("a run took longer than 3 hours; giving up")
            wall = idle_since - t0

            stats = self.server_stats() or {}
            workers = summarize_workers(self.worker_metrics(), c.workers, wall)
            rows = rows_by_action(read_counts(redis_client, started.run_id))
            snap = observer.refresh()
            result = {
                "wall_s": wall,
                "server_requests": stats.get("requests"),
                "server_distinct_pages": stats.get("distinct_pages"),
                "jobs_per_min": workers["jobs"]["success"] / wall * 60 if wall else None,
                "rows": rows,
                "table_counts": self.table_counts(engine),
                "dead_letters": sum((snap.dead_letters or {}).values()),
                "workers": workers,
                "run_id": started.run_id,
            }
            return result
        finally:
            self.stop_all()
            redis_client.close()
            engine.dispose()


def _settings_caches():
    from cageops_common.config import get_settings
    from cageops_scraper.config import get_scraper_settings
    from cageops_worker.ingest.config import get_ingest_settings

    return get_settings, get_scraper_settings, get_ingest_settings


def run_once(config: BenchConfig) -> dict:
    """Run one benchmark and append its record (valid or not) to data/bench/results.jsonl."""
    config.validate()
    ensure_bench_database(config)
    with Run(config) as run:
        result = run.execute()
        problems = check_validity(config, result, run.corpus)
        record = {
            "when": datetime.now(UTC).isoformat(timespec="seconds"),
            "config": asdict(config)
            | {"database_url": BENCH_DB, "redis_url": f"db {BENCH_REDIS_DB}"},
            "corpus": {
                "events": config.events,
                "end": CORPUS_END.isoformat(),
                "expected_cold_requests": run.corpus.expected_requests(),
            },
            "since": BACKFILL_SINCE.isoformat(),
            **result,
            "valid": not problems,
            "problems": problems,
            "hardware": hardware(),
            "git": git_state(),
        }
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS.open("a") as out:
        out.write(json.dumps(record) + "\n")
    return record


# -- the planned matrix (docs/BENCHMARKS.md) -------------------------------------------------


@dataclass
class Plan:
    runs: list[BenchConfig] = field(default_factory=list)


def matrix(events: int, urls: tuple[str, str]) -> list[BenchConfig]:
    database_url, redis_url = urls

    def cfg(workers, interval, latency, mode="cold", label=""):
        return BenchConfig(
            workers, interval, latency, mode, events, database_url=database_url,
            redis_url=redis_url, label=label,
        )  # fmt: skip

    plan = [cfg(w, 1000, 250, label="A") for w in (1, 4)]  # the limiter as we'd run it live
    plan += [cfg(w, 100, 250, label="B") for w in (1, 2, 4, 8) for _ in range(3)]
    plan += [cfg(w, 100, ms, label="C") for ms in (100, 500) for w in (1, 4)]
    plan += [cfg(4, 100, 250, label="setup")]  # leaves the cache and tables full for D and E
    plan += [cfg(w, 100, 250, "warm", "D") for w in (1, 4) for _ in range(3)]
    plan += [cfg(4, 100, 250, "rerun", "E")]
    return plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    one = commands.add_parser("run", help="one benchmark run")
    one.add_argument("--workers", type=int, required=True)
    one.add_argument("--interval-ms", type=int, required=True, help="rate-limit interval")
    one.add_argument("--latency-ms", type=float, required=True, help="simulated response time")
    one.add_argument("--mode", choices=MODES, default="cold")
    one.add_argument("--events", type=int, default=EVENTS)
    one.add_argument("--label", default="")
    every = commands.add_parser("matrix", help="the whole planned set of runs (about an hour)")
    every.add_argument("--events", type=int, default=EVENTS)
    every.add_argument("--only", help="comma-separated labels to run, e.g. B,D")
    args = parser.parse_args(argv)
    database_url, redis_url = bench_urls()

    if args.command == "run":
        configs = [
            BenchConfig(
                args.workers, args.interval_ms, args.latency_ms, args.mode, args.events,
                database_url=database_url, redis_url=redis_url, label=args.label,
            )
        ]  # fmt: skip
    else:
        configs = matrix(args.events, (database_url, redis_url))
        if args.only:
            wanted = set(args.only.split(","))
            configs = [c for c in configs if c.label in wanted]
    failures = 0
    for i, config in enumerate(configs, 1):
        print(
            f"[{i}/{len(configs)}] {config.label or 'run'}: {config.workers} worker(s), "
            f"{config.mode}, interval {config.interval_ms} ms, latency {config.latency_ms:g} ms",
            flush=True,
        )
        try:
            record = run_once(config)
        except BenchError as exc:
            print(f"   refused/failed: {exc}", flush=True)
            failures += 1
            continue
        status = "ok" if record["valid"] else "INVALID: " + "; ".join(record["problems"])
        print(
            f"   {record['wall_s']:.1f} s, {record['server_requests']} requests, "
            f"{record['jobs_per_min']:.0f} jobs/min, rows "
            f"{record['rows']['inserted']} inserted / {record['rows']['updated']} updated / "
            f"{record['rows']['unchanged']} unchanged [{status}]",
            flush=True,
        )
        failures += not record["valid"]
    return 1 if failures else 0


def median_and_range(values: list[float]) -> tuple[float, float, float]:
    return statistics.median(values), min(values), max(values)


if __name__ == "__main__":
    raise SystemExit(main())
