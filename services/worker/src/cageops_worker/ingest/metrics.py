"""Prometheus metrics for ingestion jobs. Exposed over HTTP by the worker's /metrics (checkpoint 4).

Defined on the default registry. The worker is a SimpleWorker (no fork per job, D-022), so counters
incremented inside a job are visible to /metrics in the same process.
"""

from prometheus_client import Counter, Gauge, Histogram

JOBS_TOTAL = Counter(
    "ingest_jobs_total",
    "Job attempts by outcome: success, retry (will run again) or dead_letter (gave up)",
    ["job_type", "outcome"],
)
DEAD_LETTERS_TOTAL = Counter(
    "ingest_dead_letters_total",
    "Jobs that went to the dead-letter queue, by reason",
    ["job_type", "reason"],
)
JOB_SECONDS = Histogram(
    "ingest_job_seconds",
    "Wall-clock time of one job attempt (includes waiting for a rate-limit slot)",
    ["job_type"],
    buckets=(0.05, 0.25, 1, 2, 5, 10, 30, 60, 120, 300),
)
ROWS_TOTAL = Counter(
    "ingest_rows_total",
    "Rows written by ingestion, by table and what happened to them",
    ["table", "action"],  # action: inserted | updated | unchanged
)
ENQUEUE_TOTAL = Counter(
    "ingest_enqueue_total",
    "Enqueue attempts by result (a skipped one was already queued or dead-lettered)",
    ["job_type", "result"],  # result: enqueued | already_claimed | dead_lettered
)

# -- what the worker process is doing right now (set by the worker, health.py) --------------

WORKER_HOLD = Gauge(
    "ingest_worker_hold",
    "1 while this worker is holding (taking no jobs) and why; absent while it is working",
    ["reason"],  # breaker | robots_disallowed | robots_unavailable
)
WORKER_PULSE_AGE = Gauge(
    "ingest_worker_pulse_age_seconds",
    "Seconds since this worker's loop last ticked; /healthz fails when it passes the limit",
)
