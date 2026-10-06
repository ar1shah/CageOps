"""The benchmark harness: its safety guards, its arithmetic, and one real (tiny) run end to end."""

import json

import pytest
from bench_ingest import (
    BENCH_DB,
    BenchConfig,
    BenchError,
    bench_urls,
    check_validity,
    histogram_quantile,
    is_idle,
    matrix,
    merge,
    parse_metrics,
    rows_by_action,
    run_once,
    sum_of,
    summarize_workers,
    worker_env,
)
from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest
from replay_corpus import Corpus

DB = f"postgresql://u:secretpw@localhost:5432/{BENCH_DB}"
REDIS = "redis://localhost:6379/2"


def config(**overrides) -> BenchConfig:
    values = (
        dict(workers=4, interval_ms=100, latency_ms=250.0, database_url=DB, redis_url=REDIS)
        | overrides
    )
    return BenchConfig(**values)


# -- the safety guards: the relaxed limiter can only ever apply to the replay source ----------


def test_harness_refuses_a_relaxed_interval_for_the_real_source():
    with pytest.raises(BenchError, match="only allowed for the 'ufcstats_replay' source"):
        config(source="ufcstats", interval_ms=100).validate()


@pytest.mark.parametrize("interval", [0, 100, 999])
def test_every_interval_under_the_real_floor_is_refused_for_any_other_source(interval):
    with pytest.raises(BenchError):
        config(source="ufcstats", interval_ms=interval).validate()


def test_harness_refuses_to_benchmark_the_real_source_at_all():
    with pytest.raises(BenchError, match="only run against"):
        config(source="ufcstats", interval_ms=1000).validate()


def test_the_relaxed_interval_is_fine_for_the_replay_source():
    config(interval_ms=100).validate()


def test_harness_always_runs_workers_against_the_replay_source():
    """Even if the environment it inherits says otherwise."""
    inherited = {
        "SCRAPER_SOURCE": "ufcstats",
        "UFCSTATS_BASE_URL": "http://ufcstats.com",
        "SCRAPER_MIN_INTERVAL_MS": "1000",
        "DATABASE_URL": "postgresql://u:p@localhost:5432/cageops",
        "REDIS_URL": "redis://localhost:6379/0",
        "SCRAPER_USER_AGENT": "CageOps/0.1 (+https://github.com/ar1shah/cageops; me@example.org)",
    }

    env = worker_env(config(interval_ms=100), "http://127.0.0.1:8123", 9201, base=inherited)

    assert env["SCRAPER_SOURCE"] == "ufcstats_replay"
    assert env["UFCSTATS_REPLAY_BASE_URL"] == "http://127.0.0.1:8123"
    assert env["SCRAPER_MIN_INTERVAL_MS"] == "100"
    assert env["DATABASE_URL"] == DB and env["REDIS_URL"] == REDIS  # never the dev ones
    assert env["WORKER_METRICS_PORT"] == "9201"


def test_the_replay_server_must_be_on_this_machine():
    with pytest.raises(BenchError, match="this machine"):
        config(replay_host="ufcstats.com").validate()
    with pytest.raises(BenchError, match="this machine"):
        config(replay_host="203.0.113.7").validate()


@pytest.mark.parametrize("database", ["cageops", "cageops_test", "postgres"])
def test_it_will_only_reset_the_benchmark_database(database):
    url = f"postgresql://u:p@localhost:5432/{database}"

    with pytest.raises(BenchError, match="only use 'cageops_bench'"):
        config(database_url=url).validate()


@pytest.mark.parametrize("db", [0, 1, 15])
def test_it_will_only_flush_the_benchmark_redis_database(db):
    with pytest.raises(BenchError, match="only use database 2"):
        config(redis_url=f"redis://localhost:6379/{db}").validate()


def test_the_benchmark_urls_are_derived_from_yours_without_touching_dev():
    database, redis = bench_urls(
        {
            "DATABASE_URL": "postgresql://u:pw@localhost:5432/cageops",
            "REDIS_URL": "redis://r:6379/0",
        }
    )

    assert database == f"postgresql://u:pw@localhost:5432/{BENCH_DB}"
    assert redis == "redis://r:6379/2"


def test_a_bad_mode_or_worker_count_is_refused():
    with pytest.raises(BenchError):
        config(mode="lukewarm").validate()
    with pytest.raises(BenchError):
        config(workers=0).validate()


def test_every_run_in_the_planned_matrix_is_valid():
    plan = matrix(52, (DB, REDIS))

    for c in plan:
        c.validate()
    labels = [c.label for c in plan]
    assert labels.count("A") == 2 and labels.count("B") == 12 and labels.count("C") == 4
    assert labels.count("D") == 6 and labels.count("E") == 1 and labels.count("setup") == 1
    assert {c.source for c in plan} == {"ufcstats_replay"}
    # the polite 1000 ms setting is series A only
    assert {c.interval_ms for c in plan if c.label == "A"} == {1000}


# -- reading metrics -----------------------------------------------------------------------


def worker_metrics(fetches: list[float], waits: list[float], jobs_ok: int) -> str:
    registry = CollectorRegistry()
    fetch = Histogram(
        "scraper_fetch_seconds", "x", ["source", "status_class"], registry=registry,
        buckets=(0.05, 0.1, 0.25, 0.5, 1, 2),
    )  # fmt: skip
    wait = Histogram(
        "scraper_ratelimit_wait_seconds", "x", ["source"], registry=registry, buckets=(0, 1, 2, 5)
    )
    jobs = Counter("ingest_jobs", "x", ["job_type", "outcome"], registry=registry)
    seconds = Histogram("ingest_job_seconds", "x", ["job_type"], registry=registry, buckets=(1, 5))
    for value in fetches:
        fetch.labels("ufcstats_replay", "2xx").observe(value)
    for value in waits:
        wait.labels("ufcstats_replay").observe(value)
    jobs.labels("fetch_fight", "success").inc(jobs_ok)
    seconds.labels("fetch_fight").observe(2.0)
    return generate_latest(registry).decode()


def test_metrics_are_parsed_and_added_up_across_workers():
    a = parse_metrics(worker_metrics([0.2, 0.2], [1.0], 3))
    b = parse_metrics(worker_metrics([0.3], [2.0, 2.0], 5))

    total = merge([a, b])

    assert sum_of(total, "scraper_fetch_seconds_count") == 3
    assert sum_of(total, "scraper_ratelimit_wait_seconds_sum") == 5.0
    assert sum_of(total, "ingest_jobs_total", outcome="success") == 8


def test_histogram_quantile_interpolates_inside_a_bucket():
    # 50 requests at 0.1 s (bucket 0.05-0.1) and 50 at 0.4 s (bucket 0.25-0.5)
    samples = parse_metrics(worker_metrics([0.1] * 50 + [0.4] * 50, [], 0))

    p25 = histogram_quantile(samples, "scraper_fetch_seconds", 0.25)
    p50 = histogram_quantile(samples, "scraper_fetch_seconds", 0.5)
    p75 = histogram_quantile(samples, "scraper_fetch_seconds", 0.75)

    assert p25 == pytest.approx(0.075)  # halfway through the 0.05-0.1 bucket
    assert p50 == pytest.approx(0.1)  # the top of the first bucket
    assert p75 == pytest.approx(0.375)  # halfway through the 0.25-0.5 bucket
    assert histogram_quantile({}, "scraper_fetch_seconds", 0.5) is None


def test_the_worker_summary_gives_the_amdahl_numbers():
    per_worker = [parse_metrics(worker_metrics([0.25] * 10, [0.9] * 10, 10)) for _ in range(2)]

    summary = summarize_workers(per_worker, workers=2, wall_s=20.0)

    assert summary["jobs"]["success"] == 20
    assert summary["fetches"] == 20
    assert summary["ratelimit_wait_s"] == pytest.approx(18.0)
    assert summary["ratelimit_wait_share"] == pytest.approx(18 / (2 * 20))  # 45% of worker time
    assert summary["job_time_share"] == pytest.approx(4 / 40)  # 2 s per worker over 20 s each


def test_rows_are_split_by_action_and_scheduled_bouts_are_left_out():
    counts = {
        "events:inserted": 52, "fights:inserted": 208, "fighters:inserted": 416,
        "fighters:updated": 416, "fights_scheduled:inserted": 12, "fights:unchanged": 3,
    }  # fmt: skip

    rows = rows_by_action(counts)

    assert rows["inserted"] == 52 + 208 + 416 and rows["updated"] == 416 and rows["unchanged"] == 3
    assert rows["by_table"]["fighters"] == {"inserted": 416, "updated": 416}


@pytest.mark.parametrize(
    ("depth", "idle"),
    [
        ({"queued": 0, "scheduled": 0, "started": 0, "failed": 0}, True),
        (
            {"queued": 0, "scheduled": 0, "started": 0, "failed": 7},
            True,
        ),  # dead letters aren't work
        ({"queued": 1, "scheduled": 0, "started": 0}, False),
        ({"queued": 0, "scheduled": 2, "started": 0}, False),  # a retry waiting for its delay
        ({"queued": 0, "scheduled": 0, "started": 1}, False),  # a job running right now
        (None, False),  # couldn't read Redis: never call that idle
    ],
)
def test_is_idle(depth, idle):
    assert is_idle(depth) is idle


# -- is this run's number trustworthy? ------------------------------------------------------


def good_result(corpus: Corpus, **overrides) -> dict:
    result = {
        "dead_letters": 0,
        "table_counts": {"events": 3, "fights": 12, "fighters": 24},
        "server_requests": corpus.expected_requests(),
        "rows": {"inserted": 100, "updated": 24, "unchanged": 0},
    }
    return result | overrides


def test_a_clean_cold_run_is_valid():
    corpus = Corpus(3)

    assert check_validity(config(events=3), good_result(corpus), corpus) == []


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"dead_letters": 2}, "dead letters"),
        ({"table_counts": {"events": 3, "fights": 11, "fighters": 24}}, "fights: 11 rows"),
        ({"server_requests": 99}, "server saw 99 requests"),
        ({"rows": {"inserted": 0, "updated": 0, "unchanged": 0}}, "nothing was inserted"),
    ],
)
def test_a_cold_run_that_doesnt_add_up_is_flagged(change, expected):
    corpus = Corpus(3)

    problems = check_validity(config(events=3), good_result(corpus, **change), corpus)

    assert any(expected in p for p in problems)


def test_a_warm_run_must_send_no_requests():
    corpus = Corpus(3)

    problems = check_validity(
        config(events=3, mode="warm"), good_result(corpus, server_requests=5), corpus
    )

    assert any("should use the cache" in p for p in problems)


def test_a_rerun_must_write_nothing():
    corpus = Corpus(3)
    result = good_result(
        corpus, server_requests=0, rows={"inserted": 0, "updated": 1, "unchanged": 9}
    )

    problems = check_validity(config(events=3, mode="rerun"), result, corpus)

    assert "a rerun wrote rows" in problems


# -- one real run, end to end, tiny --------------------------------------------------------


def test_a_tiny_run_cold_then_warm_then_rerun_against_the_real_stack(
    engine, redis_client, tmp_path, monkeypatch
):
    """Real server process, real worker processes, real Postgres (the cageops_bench database) and
    Redis database 2. 3 synthetic events, 20 ms latency: about 10 seconds a run."""
    import bench_ingest

    monkeypatch.setattr(bench_ingest, "RESULTS", tmp_path / "results.jsonl")
    monkeypatch.setattr(bench_ingest, "LOGS", tmp_path / "logs")
    urls = bench_urls()

    def go(workers: int, mode: str) -> dict:
        return run_once(
            BenchConfig(
                workers,
                100,
                20.0,
                mode,
                events=3,
                server_port=18097,
                database_url=urls[0],
                redis_url=urls[1],
                label="smoke",
            )
        )

    cold = go(2, "cold")
    warm = go(1, "warm")
    rerun = go(2, "rerun")

    assert cold["valid"], cold["problems"]
    assert cold["server_requests"] == Corpus(3).expected_requests() == 41
    assert cold["rows"]["inserted"] > 0 and cold["workers"]["jobs"]["dead_letter"] == 0
    assert warm["valid"], warm["problems"]
    assert warm["server_requests"] == 0 and warm["rows"]["inserted"] == cold["rows"]["inserted"]
    assert rerun["valid"], rerun["problems"]
    assert rerun["rows"]["inserted"] == 0 and rerun["rows"]["updated"] == 0
    assert rerun["rows"]["unchanged"] > 0

    lines = (tmp_path / "results.jsonl").read_text().splitlines()
    assert len(lines) == 3
    assert "secretpw" not in "".join(lines) and "@localhost" not in "".join(lines)
    record = json.loads(lines[0])
    assert record["config"]["database_url"] == BENCH_DB and record["hardware"]["logical_cpus"]
