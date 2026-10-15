"""Drive the API load test (D-046): start the API in Docker with a chosen configuration, flush the
cache, run k6 on the compose network, scrape /metrics around it, validate, and write one JSON line
per run to data/bench/api/results.jsonl.

    uv run python scripts/loadtest/prepare_db.py --force      # once: cageops_loadtest + a card
    uv run python scripts/loadtest/make_inputs.py             # once: pairs, ids, search terms
    uv run python scripts/loadtest/run_loadtest.py            # the whole matrix (about 10 minutes)
    uv run python scripts/loadtest/run_loadtest.py --only cold_16 --duration 10s

Everything shares one machine: k6, the API, Postgres and Redis compete for the same CPUs, and k6
reaches the API over the compose bridge. Numbers are not what a remote client would see.

Every run is VALIDATED (as in D-026): a cold run must have missed on every /predict and hit on none,
a warm run must hit on at least 99%, nothing may have failed, a rate run must not have dropped
iterations, a limiter-on run must not have refused anyone. A run that does not add up is recorded
as invalid with the reasons, not quietly kept.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from prometheus_client.parser import text_string_to_metric_families

ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = ROOT / "data" / "bench" / "api"
RESULTS = RESULTS_DIR / "results.jsonl"
METRICS_URL = "http://127.0.0.1:8000/metrics"
API_CONTAINER = "cageops-api-1"
LOADTEST_DB = "cageops_loadtest"
LOADTEST_REDIS_DB = 3
STAGES = ("cache", "db", "assemble", "model", "build", "serialize")


# -- what the API is started with ---------------------------------------------------------------


@dataclass(frozen=True)
class ApiConfig:
    limiter: bool = False
    pool_size: int = 5
    max_overflow: int = 5
    pool_timeout_s: float = 30  # the default before this load test; see D-041 for what it became

    def label(self) -> str:
        return (
            f"limiter={'on' if self.limiter else 'off'} pool={self.pool_size}+{self.max_overflow}"
            f" timeout={self.pool_timeout_s:g}s"
        )


@dataclass(frozen=True)
class Run:
    name: str
    scenario: str  # cold | warm | mixed | rate
    vus: int = 16
    config: ApiConfig = field(default_factory=ApiConfig)
    rate_of: str | None = None  # name of the run whose rps this one's rate is half of
    set_: str = "warm"  # for scenario "rate": which pair set
    note: str = ""
    expect_timeouts: bool = False  # a diagnostic that is MEANT to exhaust the pool


def matrix() -> list[Run]:
    off, on = ApiConfig(limiter=False), ApiConfig(limiter=True)
    return [
        Run("cold_16", "cold", note="every /predict is a pair the cache has never seen"),
        Run(
            "mixed_16",
            "mixed",
            note="predict 60% (80% warm), search 20%, profile 10%, upcoming 10%",
        ),
        # the limiter's cost: warm with it off, on, and off again, back to back
        Run("warm_16_off_1", "warm", note="limiter off"),
        Run("warm_16_on", "warm", config=on, note="limiter ON at a limit nobody reaches"),
        Run("warm_16_off_2", "warm", config=off, note="limiter off again, to see the drift"),
        Run(
            "rate_warm",
            "rate",
            rate_of="warm_16_off_1",
            set_="warm",
            note="50% of peak, open model",
        ),
        Run("rate_cold", "rate", rate_of="cold_16", set_="cold", note="50% of peak, open model"),
        # the connection pool: cold is database-heavy, 64 clients is more than the default pool
        Run("pool_64_5_5", "cold", vus=64, config=ApiConfig(pool_size=5, max_overflow=5)),
        Run("pool_64_10_10", "cold", vus=64, config=ApiConfig(pool_size=10, max_overflow=10)),
        Run("pool_64_20_20", "cold", vus=64, config=ApiConfig(pool_size=20, max_overflow=20)),
    ]


def additions() -> list[Run]:
    """Runs added after the first pass: the limiter's cost at an EQUAL arrival rate (an open model
    shows what one request pays, where a closed loop shows throughput), and a diagnostic that
    starves the pool on purpose to see the timeout path."""
    on = ApiConfig(limiter=True)
    return [
        Run(
            "rate_warm_on",
            "rate",
            rate_of="warm_16_off_1",
            set_="warm",
            config=on,
            note="the same arrival rate as rate_warm, limiter ON",
        ),  # fmt: skip
        Run(
            "pool_64_2_0_timeout_1s",
            "cold",
            vus=64,
            config=ApiConfig(pool_size=2, max_overflow=0, pool_timeout_s=1),
            expect_timeouts=True,
            note="a pool far too small, to exercise the 503 database_busy path",
        ),  # fmt: skip
        Run(
            "pool_64_2_0_timeout_50ms",
            "cold",
            vus=64,
            config=ApiConfig(pool_size=2, max_overflow=0, pool_timeout_s=0.05),
            expect_timeouts=True,
            note="the same, with a 50 ms wait: the 1 s version never timed out",
        ),
    ]


# -- docker and compose -------------------------------------------------------------------------


def dotenv() -> dict[str, str]:
    values: dict[str, str] = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    return values


def compose(*args: str, env: dict[str, str] | None = None, capture: bool = True):
    return subprocess.run(
        ["docker", "compose", *args],
        cwd=ROOT,
        env={**os.environ, **(env or {})},
        capture_output=capture,
        text=True,
        check=False,
    )


def api_env(config: ApiConfig) -> dict[str, str]:
    d = dotenv()
    user, password = d["POSTGRES_USER"], d["POSTGRES_PASSWORD"]
    return {
        "API_DATABASE_URL": f"postgresql://{user}:{password}@postgres:5432/{LOADTEST_DB}",
        "API_REDIS_URL": f"redis://redis:6379/{LOADTEST_REDIS_DB}",
        "CACHE_ENABLED": "true",
        "RATE_LIMIT_ENABLED": "true" if config.limiter else "false",
        # "high": so high that a single client at full speed never reaches it
        "RATE_LIMIT_PER_MINUTE": "600000000",
        "RATE_LIMIT_BURST": "100000000",
        "DB_POOL_SIZE": str(config.pool_size),
        "DB_MAX_OVERFLOW": str(config.max_overflow),
        "DB_POOL_TIMEOUT_S": str(config.pool_timeout_s),
        "LOG_LEVEL": "WARNING",  # access lines are INFO: keep logging out of the measurement
    }


def start_api(config: ApiConfig) -> None:
    result = compose(
        "--profile", "api", "up", "-d", "--force-recreate", "--no-deps", "api", env=api_env(config)
    )
    if result.returncode != 0:
        raise SystemExit(f"could not start the API:\n{result.stderr}")
    deadline = time.time() + 90
    while time.time() < deadline:
        status = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Health.Status}}", API_CONTAINER],
            capture_output=True, text=True, check=False,
        ).stdout.strip()  # fmt: skip
        if status == "healthy":
            return
        time.sleep(1)
    raise SystemExit("the API did not become healthy in 90 s")


def flush_cache() -> None:
    result = compose("exec", "-T", "redis", "redis-cli", "-n", str(LOADTEST_REDIS_DB), "FLUSHDB")
    if "OK" not in result.stdout:
        raise SystemExit(
            f"could not flush Redis db {LOADTEST_REDIS_DB}: {result.stdout} {result.stderr}"
        )


# -- metrics ------------------------------------------------------------------------------------

Snapshot = dict[tuple[str, tuple[tuple[str, str], ...]], float]


def scrape() -> Snapshot:
    with urllib.request.urlopen(METRICS_URL, timeout=5) as response:
        text = response.read().decode()
    snapshot: Snapshot = {}
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            snapshot[(sample.name, tuple(sorted(sample.labels.items())))] = sample.value
    return snapshot


def total(snap: Snapshot, name: str, **labels: str) -> float:
    return sum(
        v
        for (n, ls), v in snap.items()
        if n == name and all(dict(ls).get(k) == want for k, want in labels.items())
    )


def delta(after: Snapshot, before: Snapshot, name: str, **labels: str) -> float:
    return total(after, name, **labels) - total(before, name, **labels)


def histogram_delta(after: Snapshot, before: Snapshot, base: str, **labels: str):
    """[(upper_bound, cumulative_count_delta)] for histogram `base`, summed over label sets that
    match, sorted by bound."""
    buckets: dict[float, float] = {}
    for (name, ls), value in after.items():
        d = dict(ls)
        if name != f"{base}_bucket" or any(d.get(k) != want for k, want in labels.items()):
            continue
        le = math.inf if d["le"] == "+Inf" else float(d["le"])
        prior = before.get((name, ls), 0.0)
        buckets[le] = buckets.get(le, 0.0) + (value - prior)
    return sorted(buckets.items())


def quantile(q: float, buckets) -> float | None:
    """Prometheus's histogram_quantile: find the bucket the rank falls in, interpolate linearly."""
    if not buckets or buckets[-1][1] <= 0:
        return None
    rank = q * buckets[-1][1]
    previous_bound, previous_count = 0.0, 0.0
    for bound, count in buckets:
        if count >= rank:
            if math.isinf(bound):
                return previous_bound
            width = bound - previous_bound
            in_bucket = count - previous_count
            return previous_bound + width * (
                (rank - previous_count) / in_bucket if in_bucket else 0
            )
        previous_bound, previous_count = bound, count
    return None


def ms(value: float | None) -> float | None:
    return None if value is None else round(value * 1000, 2)


class Sampler(threading.Thread):
    """Reads the instantaneous pool gauge every 50 ms, as a cross-check on the server's own
    high-water mark (which is read by the scrapes themselves)."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.peak_checked_out = 0.0
        self.peak_server = 0.0
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                snap = scrape()
                self.peak_checked_out = max(
                    self.peak_checked_out, total(snap, "api_db_pool_checked_out")
                )
                self.peak_server = max(
                    self.peak_server, total(snap, "api_db_pool_checked_out_peak")
                )
            except OSError:
                pass
            self._stop_event.wait(0.05)

    def stop(self) -> None:
        self._stop_event.set()
        self.join(5)


# -- k6 -----------------------------------------------------------------------------------------


def run_k6(name: str, env: dict[str, str], log: Path) -> dict:
    summary = RESULTS_DIR / f"{name}.summary.json"
    summary.unlink(missing_ok=True)
    args = [
        "--profile", "api", "--profile", "loadtest", "run", "--rm", "-T", "--no-deps",
    ]  # fmt: skip
    for key, value in env.items():
        args += ["-e", f"{key}={value}"]
    args += [
        "k6",
        "run",
        "--quiet",
        "--summary-export",
        f"/results/{summary.name}",
        "/scripts/api.js",
    ]
    result = compose(
        *args, env={"LOADTEST_UID": str(os.getuid()), "LOADTEST_GID": str(os.getgid())}
    )
    log.write_text(result.stdout + "\n--- stderr ---\n" + result.stderr)
    if not summary.exists():
        raise SystemExit(f"k6 produced no summary for {name}; see {log}\n{result.stderr[-800:]}")
    return json.loads(summary.read_text())


def k6_metric(summary: dict, name: str) -> dict:
    return summary.get("metrics", {}).get(name, {})


def k6_trend(summary: dict, name: str) -> dict | None:
    m = k6_metric(summary, name)
    if "med" not in m:
        return None
    return {
        "count": None,
        "avg": round(m.get("avg", 0), 2),
        "p50": round(m["med"], 2),
        "p90": round(m.get("p(90)", 0), 2),
        "p95": round(m.get("p(95)", 0), 2),
        "p99": round(m.get("p(99)", 0), 2),
        "max": round(m.get("max", 0), 2),
    }


# -- one run ------------------------------------------------------------------------------------


def execute(run: Run, duration: str, rates: dict[str, float], k6_rps: dict[str, float]) -> dict:
    flush_cache()
    base_env = {"VUS": str(run.vus), "DURATION": duration}
    if run.scenario in ("warm", "mixed") or (run.scenario == "rate" and run.set_ == "warm"):
        # one pass over the warm set, unmeasured, then the cache really does hold those pairs
        run_k6(f"{run.name}.warmup", {**base_env, "SCENARIO": "warm", "PHASE": "warmup"},
               RESULTS_DIR / f"{run.name}.warmup.log")  # fmt: skip
        if run.scenario == "mixed":  # and the upcoming card, so its first request is not measured
            urllib.request.urlopen("http://127.0.0.1:8000/events/upcoming", timeout=30).read()

    env = {**base_env, "SCENARIO": run.scenario, "PHASE": "measure"}
    if run.scenario == "rate":
        target = k6_rps[run.rate_of] * 0.5
        env |= {"RATE": str(max(1, round(target))), "SET": run.set_, "MIX": "predict"}
        if run.set_ == "cold":
            flush_cache()
    rate = int(env.get("RATE", 0))

    before = scrape()
    sampler = Sampler()
    sampler.start()
    started = time.time()
    summary = run_k6(run.name, env, RESULTS_DIR / f"{run.name}.log")
    elapsed = time.time() - started
    sampler.stop()
    after = scrape()

    # -- what k6 saw
    reqs = k6_metric(summary, "http_reqs")
    failed = k6_metric(summary, "http_req_failed")
    endpoints = {}
    for endpoint in ("predict", "search", "profile", "upcoming"):
        trend = k6_trend(summary, f"http_req_duration{{name:{endpoint}}}")
        count = k6_metric(summary, f"http_reqs{{name:{endpoint}}}").get("count")
        if trend and count:
            trend["count"] = int(count)
            endpoints[endpoint] = trend
    k6_block = {
        "requests": int(reqs.get("count", 0)),
        "rps": round(reqs.get("rate", 0.0), 1),
        "failed_rate": failed.get("value", failed.get("rate", 0.0)),
        "dropped_iterations": int(k6_metric(summary, "dropped_iterations").get("count", 0)),
        "all": k6_trend(summary, "http_req_duration"),
        "by_endpoint": endpoints,
    }
    k6_rps[run.name] = k6_block["rps"]

    # -- what the server saw (deltas over the measured window)
    predict_hist = histogram_delta(
        after, before, "api_request_duration_seconds", route="/predict", status="200"
    )
    server = {
        "predict_latency_ms": {
            "p50": ms(quantile(0.50, predict_hist)),
            "p95": ms(quantile(0.95, predict_hist)),
            "p99": ms(quantile(0.99, predict_hist)),
        },
        "cache": {
            "hits": int(delta(after, before, "api_cache_hits_total")),
            "misses": int(delta(after, before, "api_cache_misses_total")),
            "bypassed": int(delta(after, before, "api_cache_bypassed_total")),
            "redis_errors": int(delta(after, before, "api_redis_errors_total")),
        },
        "ratelimited": int(delta(after, before, "api_ratelimited_total")),
        "stage_avg_ms": {},
    }
    for stage in STAGES:
        count = delta(after, before, "api_predict_stage_seconds_count", stage=stage)
        if count:
            sec = delta(after, before, "api_predict_stage_seconds_sum", stage=stage)
            server["stage_avg_ms"][stage] = round(sec / count * 1000, 3)
    wait = histogram_delta(after, before, "api_db_pool_checkout_seconds")
    server["pool"] = {
        "size": run.config.pool_size,
        "max_overflow": run.config.max_overflow,
        "peak_checked_out": int(max(sampler.peak_server, sampler.peak_checked_out)),
        "peak_checked_out_server_high_water": int(sampler.peak_server),
        "peak_checked_out_sampled": int(sampler.peak_checked_out),
        "checkouts": int(delta(after, before, "api_db_pool_checkout_seconds_count")),
        "wait_ms": {
            "p50": ms(quantile(0.5, wait)),
            "p95": ms(quantile(0.95, wait)),
            "p99": ms(quantile(0.99, wait)),
        },  # fmt: skip
        "timeouts": int(delta(after, before, "api_db_pool_checkout_timeouts_total")),
    }

    # -- validation
    problems: list[str] = []
    predicts = endpoints.get("predict", {}).get("count", 0)
    hits, misses = server["cache"]["hits"], server["cache"]["misses"]
    if run.expect_timeouts:
        if not server["pool"]["timeouts"]:
            problems.append("the pool was meant to be exhausted and no checkout timed out")
        # every failure should be a pool timeout answered 503, and nothing else
        failed_requests = round(k6_block["failed_rate"] * k6_block["requests"])
        if failed_requests != server["pool"]["timeouts"]:
            problems.append(
                f"{failed_requests} requests failed but {server['pool']['timeouts']} checkouts "
                "timed out"
            )
    elif k6_block["failed_rate"] > 0:
        problems.append(f"{k6_block['failed_rate']:.4%} of requests failed")
    if run.scenario == "cold":
        if hits != 0:
            problems.append(f"cold run had {hits} cache hits")
        if misses != predicts:
            problems.append(f"cold run: {misses} misses for {predicts} predictions")
    if run.scenario == "warm" and (hits + misses == 0 or hits / (hits + misses) < 0.99):
        problems.append(f"warm run hit ratio {hits}/{hits + misses} is under 99%")
    if run.scenario == "rate":
        if k6_block["dropped_iterations"]:
            problems.append(
                f"{k6_block['dropped_iterations']} iterations dropped: "
                "the generator could not keep the rate"
            )
        if abs(k6_block["rps"] - rate) > 0.05 * rate:
            problems.append(f"achieved {k6_block['rps']} rps for a target of {rate}")
    if run.config.limiter and server["ratelimited"]:
        problems.append(f"limiter refused {server['ratelimited']} requests")
    if not run.config.limiter and server["ratelimited"]:
        problems.append("requests were rate limited with the limiter off")
    if server["cache"]["redis_errors"]:
        problems.append(f"{server['cache']['redis_errors']} Redis errors")

    return {
        "name": run.name,
        "scenario": run.scenario,
        "note": run.note,
        "when": datetime.now(UTC).isoformat(timespec="seconds"),
        "vus": run.vus,
        "target_rate": rate or None,
        "duration_requested": duration,
        "duration_measured_s": round(elapsed, 1),
        "api": run.config.label(),
        "k6": k6_block,
        "server": server,
        "valid": not problems,
        "problems": problems,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--only", nargs="*", help="run names to run (default: the whole matrix)")
    parser.add_argument("--duration", default="30s")
    parser.add_argument("--fresh", action="store_true", help="truncate the results file first")
    parser.add_argument("--results", type=Path, default=RESULTS)
    args = parser.parse_args()
    results = args.results

    if not (RESULTS_DIR / "inputs.json").exists():
        raise SystemExit("run scripts/loadtest/make_inputs.py first")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    if args.fresh:
        results.write_text("")

    runs = [r for r in matrix() + additions() if not args.only or r.name in args.only]
    # a rate run needs its parent's rps: take it from the results file if it was run earlier
    k6_rps: dict[str, float] = {}
    # a rate run needs its parent's rps: the main results file has them even for a side file
    for source in {RESULTS, results}:
        if not source.exists():
            continue
        for line in source.read_text().splitlines():
            if line.strip():
                rec = json.loads(line)
                k6_rps[rec["name"]] = rec["k6"]["rps"]

    current: ApiConfig | None = None
    for run in runs:
        if run.config != current:
            print(f"starting the API: {run.config.label()}", flush=True)
            start_api(run.config)
            current = run.config
        print(f"run {run.name}: {run.scenario} vus={run.vus} for {args.duration}", flush=True)
        record = execute(run, args.duration, {}, k6_rps)
        with results.open("a") as fh:
            fh.write(json.dumps(record) + "\n")
        flag = "valid" if record["valid"] else "INVALID: " + "; ".join(record["problems"])
        print(f"  {record['k6']['rps']} rps, predict p50/p95 (k6) "
              f"{(record['k6']['by_endpoint'].get('predict') or {}).get('p50')}/"
              f"{(record['k6']['by_endpoint'].get('predict') or {}).get('p95')} ms, {flag}",
              flush=True)  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main())
