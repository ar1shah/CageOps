# Runbook

How to operate CageOps. Written so someone can redo everything without asking.

## Local development

Prerequisites: Docker, uv.

```bash
cp -n .env.example .env          # create your local env file (never overwrites an existing one)
docker compose up -d             # start Postgres (pgvector) + Redis
docker compose ps                # both should show "healthy"
uv sync --all-packages --all-groups   # install Python deps for every package
uv run pytest                    # run all tests
uv run ruff check . && uv run ruff format .   # lint + format
```

Run the API locally:

```bash
uv run --package cageops-api uvicorn cageops_api.main:app --port 8000
curl localhost:8000/healthz
```

Stop / reset:

```bash
docker compose down              # stop containers, KEEP data (volumes survive)
docker compose down -v           # stop containers and DELETE all local data
docker compose logs -f postgres  # watch a service's logs
```

## Seed data

Needs Kaggle credentials in `~/.kaggle/` (outside the repo; never commit them).

```bash
scripts/download_seed.sh          # download missing files into data/raw/, write the manifest
scripts/download_seed.sh --force  # re-download everything
```

This writes `data/raw/MANIFEST.json` (local) and `docs/seed_manifest.json` (committed). If row counts differ from an earlier run, compare the sha256 values in the manifest first: a changed hash means Kaggle's data changed, not our code.

## Database and seed load

```bash
uv run alembic -c packages/cageops_common/alembic.ini upgrade head      # create / update the schema
uv run alembic -c packages/cageops_common/alembic.ini check             # fail if models and migrations drifted
uv run alembic -c packages/cageops_common/alembic.ini downgrade -1      # undo the latest migration
uv run python -m cageops_worker.seed                                    # load seed data (idempotent)
```

Downgrades work on an empty database (a test checks this). On a loaded one, going back past
migration 0002 fails on purpose, because some fights have no weight class. To start over, reset
the schema (`docker compose down -v`, or drop and recreate the `public` schema) and re-run the
migrations and the loader; the result is identical.

The loader checks every input file's sha256 against `docs/seed_manifest.json` and refuses to
run on a mismatch. It prints a JSON report per source: rows read / loaded / rejected, what
didn't match and why. Every run is also recorded in the `load_runs` table with the file hash.

Tests that touch the database use a separate `cageops_test` database on the same server and
skip with a reason if Postgres isn't running. To make an unreachable database a failure
(like CI does), run `REQUIRE_DB=1 uv run pytest`.

## Ingestion

Every command is `uv run python -m cageops_worker.ingest <command>` (`--help` on any of them). The worker runs on Linux or macOS only (WSL2 or Docker on Windows): RQ's job timeouts use SIGALRM (D-022).

| Command | What it does |
|---|---|
| `backfill --since YYYY-MM-DD` (or `--months N`) `[--force]` | queue every completed event since then; prints a run id |
| `scrape-upcoming [--force]` | queue a read of the upcoming events |
| `worker [--burst] [--max-jobs N] [--metrics-port P]` | run one worker; serves `/healthz` and `/metrics` on port 9100 by default |
| `status [--run RUN_ID] [--json]` | queue depth, dead letters by reason, breaker, and a run's inserted / updated / unchanged rows |
| `dlq list [--reason R]`, `dlq inspect <job_id>` | what failed for good, and why |
| `dlq replay <job_id>` or `dlq replay --all [--reason R]` | send dead letters around again with a fresh retry budget |
| `dlq purge <job_id>` | give up on one |
| `breaker status`, `breaker reset --yes` | see or clear the circuit breaker |

Exit codes for one-shot commands: 0 ok, 1 error, 2 bad usage, 3 refused (the site is blocking us, robots.txt says no, or the platform can't run the worker). A long-running `worker` never exits for those reasons; it holds (see "The site is blocking us" below).

### Triage: what to do when

- **`status` shows dead letters.** `dlq list` groups them by reason. `not_found` is a page that doesn't exist (often fine). `parse_error` or `mapping_error` means the site's markup or a value changed: read `dlq inspect <id>` (it shows the URL, error and traceback), fix the parser or mapping, then `dlq replay --all --reason parse_error`. `retries_exhausted` means the site or our network kept failing: once it is healthy, `dlq replay --all --reason retries_exhausted`. `source_blocked` means the breaker tripped: see below.
- **The site is blocking us.** Symptoms: `scraper_breaker_open` is 1, `breaker status` says OPEN, workers report `worker_state: hold:breaker` in `/healthz`, and the queue stops moving. The pods are *healthy on purpose* (restarting can't clear a block), so only this metric tells you. Do **not** reset it to "see if it works": a site that told us to go away and is hit again may block harder. Open the site in a browser, read what it says, and if appropriate email the operator (D-013). When access is genuinely back, `breaker reset --yes`; workers resume within `WORKER_HOLD_POLL_S` seconds; then `dlq replay --all --reason source_blocked`. We never use headless browsers, challenge solvers or proxy rotation (D-013).
- **A worker is unhealthy (`/healthz` 503).** The pulse is older than `INGEST_JOB_TIMEOUT_S + WORKER_HEALTH_MARGIN_S`: the worker's loop is wedged (a job stuck somewhere RQ's timeout can't interrupt). Restart it; the job it was running is recovered by RQ and retried. Redis or Postgres being down does **not** make `/healthz` fail; read the JSON body (`dependencies`) instead.
- **Weekly refresh (required before Phase 5).** `backfill --months 6 --force` re-reads the last six months, so results overturned after the 3-day cache window are corrected (D-015 amendment). About 300 requests, 5 minutes at 1 request/second.

### Try it end to end without touching ufcstats.com

This runs the whole pipeline against `scripts/standin_site.py`, a tiny local server that serves our saved fixture pages at the real site's URL paths. It uses a throwaway database and Redis database 1, so your seed data and dev queue are not touched. **Nothing here sends a request to ufcstats.com.** Most events, fights and fighters on the saved list pages have no fixture, so you will see `not_found` dead letters; that is the small fixture set, not a bug.

Prerequisites: `docker compose up -d`, `uv sync --all-packages --all-groups`, and a real contact address in `SCRAPER_USER_AGENT` in `.env` (the scraper refuses to start with the placeholder).

**1. Start the stand-in site** (terminal A; leave it running, it prints every request):

```bash
uv run python scripts/standin_site.py
```

**2. Set up a throwaway database and the environment** (terminal B; keep using this terminal). Replace `change-me-locally` with your `POSTGRES_PASSWORD` from `.env`:

```bash
docker compose exec postgres createdb -U cageops cageops_demo
export DATABASE_URL=postgresql://cageops:change-me-locally@localhost:5432/cageops_demo
export REDIS_URL=redis://localhost:6379/1
export SCRAPER_SOURCE=ufcstats_replay UFCSTATS_REPLAY_BASE_URL=http://127.0.0.1:8099
export SCRAPER_MIN_INTERVAL_MS=200 INGEST_RETRY_BASE_S=0
uv run alembic -c packages/cageops_common/alembic.ini upgrade head
```

(`ufcstats_replay` is its own source: its own cache, rate limit and circuit breaker. `INGEST_RETRY_BASE_S=0` makes retries instant so a failure reaches the dead-letter queue in seconds instead of minutes.)

**3. Look at an empty system.** Expect: source `ufcstats_replay`, queue all 0, `dead letters  none`, `breaker  closed`.

```bash
uv run python -m cageops_worker.ingest status
```

**4. First run: queue a backfill and work it.** The first command prints `Queued a backfill ... as run <RUN_ID>`; copy the id. The worker prints JSON log lines and exits by itself when the queue is empty (`--burst`).

```bash
uv run python -m cageops_worker.ingest backfill --since 2026-04-18
uv run python -m cageops_worker.ingest worker --burst --metrics-port 0
```

**5. Read the result.** Expect `inserted` counts for `events`, `fights`, `fight_totals`, `fight_round_stats` and `fighters`, and some `not_found` dead letters. Replace `<RUN_ID>`.

```bash
uv run python -m cageops_worker.ingest status --run <RUN_ID>
```

**6. Run it again: nothing new.** Note the stand-in's request count first, then repeat the backfill and worker. Expect the new run's `status --run` to show only `unchanged` (no `inserted`, no `updated`), and the stand-in's request count to be unchanged (everything came from the cache).

```bash
curl localhost:8099/__stats
uv run python -m cageops_worker.ingest backfill --since 2026-04-18
uv run python -m cageops_worker.ingest worker --burst --metrics-port 0
curl localhost:8099/__stats
```

Then `status --run <NEW_RUN_ID>` as in step 5.

**7. Break the site, and watch a job end up in the dead-letter queue.** `--force` skips the cache so the broken site is actually hit. Expect `retries_exhausted` with 6 attempts.

```bash
curl -X POST localhost:8099/__mode/fail
uv run python -m cageops_worker.ingest backfill --since 2026-04-18 --force
uv run python -m cageops_worker.ingest worker --burst --metrics-port 0
uv run python -m cageops_worker.ingest dlq list --reason retries_exhausted
uv run python -m cageops_worker.ingest dlq inspect discover-completed-p1-since-2026-04-18
```

**8. Fix the site and replay.** Expect `replayed 1`, then an empty list.

```bash
curl -X POST localhost:8099/__mode/ok
uv run python -m cageops_worker.ingest dlq replay --all --reason retries_exhausted
uv run python -m cageops_worker.ingest worker --burst --metrics-port 0
uv run python -m cageops_worker.ingest dlq list --reason retries_exhausted
```

**9. A long-running worker: health, metrics, and what it does when the site blocks us.** In terminal C start a worker that keeps running (same environment variables as step 2):

```bash
uv run python -m cageops_worker.ingest worker
```

Back in terminal B. Expect `/healthz` to answer 200 with `"worker_state": "running"`, and `/metrics` to show `ingest_queue_depth`, `ingest_dead_letters_current`, `scraper_breaker_open{...} 0.0` and `ingest_jobs_total`:

```bash
curl localhost:9100/healthz
curl -s localhost:9100/metrics | grep -E "^(ingest_queue_depth|ingest_dead_letters_current|scraper_breaker_open|ingest_jobs_total)"
```

Now make the site serve the bot challenge and queue a forced backfill. Expect the breaker to trip within a second or two: terminal C logs `holding: not taking jobs` and the worker **stays running**; `/healthz` stays 200 but says `hold:breaker`; `scraper_breaker_open` is `1.0`; one dead letter has reason `source_blocked`; and a new backfill is refused (exit code 3) instead of queued.

```bash
curl -X POST localhost:8099/__mode/challenge
uv run python -m cageops_worker.ingest backfill --since 2026-04-18 --force
curl localhost:9100/healthz
curl -s localhost:9100/metrics | grep -E "^(scraper_breaker_open|ingest_worker_hold)"
uv run python -m cageops_worker.ingest status
uv run python -m cageops_worker.ingest backfill --since 2026-04-18
```

Make the site healthy again, clear the breaker (this is the human decision), replay the blocked job and watch terminal C resume (up to 30 seconds):

```bash
curl -X POST localhost:8099/__mode/ok
uv run python -m cageops_worker.ingest breaker reset --yes
uv run python -m cageops_worker.ingest dlq replay --all --reason source_blocked
uv run python -m cageops_worker.ingest status
```

Stop the worker in terminal C with Ctrl-C: it finishes the current job and exits.

**10. Clean up.** Stop the stand-in site (Ctrl-C in terminal A), then:

```bash
docker compose exec postgres dropdb -U cageops cageops_demo
docker compose exec redis redis-cli -n 1 FLUSHDB
```

and close terminal B (or `unset DATABASE_URL REDIS_URL SCRAPER_SOURCE UFCSTATS_REPLAY_BASE_URL SCRAPER_MIN_INTERVAL_MS INGEST_RETRY_BASE_S`) so later commands use your real `.env` again.

### Benchmark the pipeline (replay server only)

Needs `docker compose up -d` and a migrated database (the harness creates and migrates its own `cageops_bench` database and uses Redis database 2, so your dev data is never touched; it refuses to reset anything else). Everything runs on this machine against the replay server: **the numbers are replay numbers, never the real site's.** Don't run other heavy work while it runs, it shares the CPUs.

```bash
uv run python scripts/bench_ingest.py run --workers 4 --interval-ms 100 --latency-ms 250
uv run python scripts/bench_ingest.py run --workers 4 --interval-ms 100 --latency-ms 250 --mode warm
uv run python scripts/bench_ingest.py run --workers 4 --interval-ms 100 --latency-ms 250 --mode rerun
uv run python scripts/bench_ingest.py matrix --only B,D
```

- `cold`: nothing cached, every page comes from the replay server. `warm`: pages are in `raw_pages`, the data tables are emptied, so the same rows are re-inserted with no network (the harness asserts zero requests). `rerun`: cache and tables both full; asserts nothing is inserted or updated. A warm or rerun run needs an earlier cold run's cache and rows.
- A run prints one line (seconds, requests, jobs/minute, rows inserted / updated / unchanged) and appends a full record (including the CPU, commit and the workers' limiter-wait share) to `data/bench/results.jsonl`. A run whose numbers don't add up (dead letters, wrong row counts, unexpected requests) is recorded with `valid: false` and the reasons; don't quote it.
- The rate-limit interval under 1000 ms is accepted only for the replay source; the harness and `ScraperSettings` both refuse it for the real site, and the replay URL must be on this machine.
- Logs of every process of a run are in `data/bench/logs/`.

## Production

_Written in Phase 2c (VM + docker compose) and rewritten in Phase 6 (k3s)._

## Incidents

_Record what broke, how it was found, and how it was fixed._
