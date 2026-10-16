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

### Wikipedia results (Phase 1d: fill a gap while ufcstats is blocked)

English Wikipedia's UFC event articles give results (winner, method, round, time) and no stats. We fetch only `/wiki/<Title>` article pages, which Wikipedia's robots.txt allows (D-028); the source refuses any other URL. Needs `docker compose up -d`, a migrated database, a seed load and a real contact email in `SCRAPER_USER_AGENT` (Wikimedia requires one). One source per worker process, each with its own queue:

```bash
export SCRAPER_SOURCE=wikipedia INGEST_QUEUE=ingest-wikipedia
uv run python -m cageops_worker.ingest backfill --since 2026-05-17
uv run python -m cageops_worker.ingest worker --burst
uv run python -m cageops_worker.ingest status --run <RUN_ID>
uv run python -m cageops_worker.ingest dlq list
```

The first backfill is about 25 requests (one list page per year plus one article per event), about a minute at 1 request/second. **Say what will be fetched before running it**, and add the requests and the revisions it read to the request log in D-013.

- **`status --run` lists two things to read.** "Revisions read" is which revision of each article the results came from. "Fighters created from Wikipedia" is every fighter that matched nobody, with the link they came from: **read it for spelling variants** (a nickname, a married name, a transliteration). The check below cannot catch those.
- **Held events.** An event whose article has a fighter name that looks like an existing fighter ends in `dlq list` as a `mapping_error`, and nothing from that event is written. `dlq inspect wiki-event-UFC_331` shows the name, its article link and the fighters it looks like, with why. Decide from evidence (same weight class and era in the seed, the article's own bout), never from the spelling alone:
  - **It is the existing fighter:** add a row to `services/worker/src/cageops_worker/seed/aliases.csv`: `wikipedia,<name as written in the article>,<their ufcstats_id>,<the evidence>`. Find the id with `SELECT ufcstats_id, name FROM fighters WHERE name ILIKE '%surname%'`. Two fighters with one name: choose by weight class.
  - **It is a new fighter:** add a row to `services/worker/src/cageops_worker/ingest/wikipedia_distinct_fighters.csv`: `<link title or Name_With_Underscores>,<the reason>`.
  - Then `uv run python -m cageops_worker.ingest dlq replay --all` and `uv run python -m cageops_worker.ingest worker --burst`. No seed re-run is needed, and the replay makes no requests (the pages are cached). Repeat until `dlq list` is empty: an event can hold several names, and the check reports the first it meets.
  - **Then rebuild the features** (`uv run python -m cageops_worker.features rebuild`). A replay re-parses cached pages, so fights change with no new fetch, and `train` will refuse to run on features built before it.
- **Precedence.** A ufcstats result is never replaced; Wikipedia fills a scheduled ufcstats bout that has no result; anomalies `source_disagreement`, `result_changed` and `ufcstats_scheduled_without_wikipedia_match` appear in the worker log. Nothing is ever cancelled by this source.
- **Refreshing.** An event article is refetched after 24 hours until 30 days after the event, so a later edit is picked up by `backfill --since <today minus 30 days>`; older articles need `--force`. `scrape-upcoming` is refused for this source.
- **After a run:** `uv run python -m cageops_worker.features rebuild` (history now runs through the latest Wikipedia event). Wikipedia fights count as fights but carry no stats.
- **A new fixture page** (a real request, so announce it first): fetch it through the fetcher into `raw_pages`, save the HTML under the gitignored `data/fixtures/wikipedia/`, then `uv run python scripts/minimize_wikipedia_fixture.py data/fixtures/wikipedia/<page>.html --out services/scraper/tests/fixtures/wikipedia` and add the article to that directory's NOTICE (the fixtures are CC BY-SA 4.0).

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

## Features

The `fight_features` table holds, for every fight and each of its two fighters, what was known **before** that fight (D-027). It is derived, so it can be rebuilt from `fights`, `events`, `fighters` and `fight_totals` at any time. Needs `docker compose up -d` and a migrated, seeded database.

```bash
uv run python -m cageops_worker.features rebuild
uv run python -m cageops_worker.features rebuild --today 2026-10-06
```

- `rebuild` recomputes every row in one transaction and prints a JSON report: row counts, `history_through` (the latest completed event we hold), how many completed fights have no trusted length (by reason), any past-dated `scheduled` fights it left out, and `output_sha256` (the same data always gives the same hash). If a check fails (every fight must produce exactly 2 rows) it exits 1, prints the report on stderr and leaves the old table untouched. A failed run leaves no `load_runs` row, same as the seed loaders.
- `--today` pins what "upcoming" means (scheduled fights dated before it are left out) and the date `fighter_features_current` is as of. The default is today **in `APP_TIMEZONE`** (America/New_York unless set), not the host's local date: event dates are US-local, so a Saturday card stays upcoming until midnight there (D-037).
- The same transaction also rewrites `fighter_features_current` (one row per fighter, as of the build date; this is what the API reads for an ad-hoc matchup). It stores `dob` and `last_fight_date`, not `age_days` or `days_since_last_fight`: those two depend on the date being asked about and are derived when needed. The report adds `fighters_current`, `current_as_of` and `current_output_sha256`; `output_sha256` still describes `fight_features` only.
- Rebuild after any seed load or ingestion run, and after a result is overturned (a rebuild picks up the corrected result).
- The report also has `inputs_sha256`: a hash of the completed fights, their dates, their fighters' bios and their whole-fight stats, which is what the build read (D-033). `train` recomputes it and refuses to train when it no longer matches, so a rebuild is required after any change to those, including a Wikipedia replay (see below). `output_sha256` is unchanged by it.

Look at one fighter's rows, oldest first (name matching ignores case and accents; an ambiguous name lists the candidates and exits 2):

```bash
uv run python -m cageops_worker.features show --fighter "Israel Adesanya"
uv run python -m cageops_worker.features show --fighter-id 1234 --json
```

Spot-check against what you know: the first row should say 0 prior fights, and `win_streak` should reset to 0 right after a loss.

Who is missing per-round stats, and why (read-only; needs no rebuild):

```bash
uv run python -m cageops_worker.features stats-gap --since 2026-05-17
uv run python -m cageops_worker.features stats-gap --json
```

- A fight counts as having stats only when **both** fighters have a `fight_totals` row, the same test the builder uses. The report splits fighters by who supplied the stat-less result (`fights.result_source`): `wikipedia_only` are the ones whose recent fights came from Wikipedia (D-025, D-029); `ufcstats_only` are old fights the source never had numbers for (21 fights from the 1990s on 2026-10-15).
- `--since 2026-05-17` reproduces the ROADMAP's query: fighters with a completed fight on or after that date (406 on 2026-10-15), and how many of them have a fight lacking stats (also 406). Without `--since`, 442 fighters had a gap: 406 Wikipedia-only plus 36 ufcstats-only.

## Training

Two LightGBM models (win/loss and method of victory) are trained, scored against the baselines and registered in `model_versions` as one version (D-031, D-032, D-033). Needs `docker compose up -d`, migration 0009 (`uv run alembic -c packages/cageops_common/alembic.ini upgrade head`) and a **fresh features build** (`uv run python -m cageops_worker.features rebuild`; the build must be made by code that stores `inputs_sha256`, so rebuild once after upgrading).

```bash
uv run python -m cageops_trainer train
uv run python -m cageops_trainer train --seed 7 --val-start 2024-10-04 --test-start 2025-10-04
uv run python -m cageops_trainer list
uv run python -m cageops_trainer activate 20261014-225801
```

- **`train`** reads everything in one snapshot, splits by time, fits both models, scores them, writes `MODEL_ARTIFACT_DIR/<version>/` (`win_loss.txt`, `method.txt`, `manifest.json`) and registers the version **inactive**. It never activates. About 5 seconds on the benchmark machine (BENCHMARKS.md). Stdout is the JSON report; stderr gets plain-English summary lines and any warnings.
  - `--val-start` / `--test-start` default to the last two 365-day windows of data; `--train-start` ignores older fights. Whatever was used is stored in the row.
  - `--seed` (default 42) sets every random choice; the same seed on the same data and library versions gives byte-identical model files.
- **It refuses (exit 1, "run features rebuild")** when `fight_features` is not one complete build (two rows for every fight with a winner), when the build predates the input fingerprint, or when the fights, bios or stats it was built from have changed since (an overturned result, a filled-in bio, a Wikipedia replay). Nothing is written. The fix is always `uv run python -m cageops_worker.features rebuild`, then `train` again.
- **It warns but still trains** when a seed load or a page fetch happened after the build and changed nothing the features use. The warning is in the report's `warnings` and on stderr. Seeing it is normal once scraping is scheduled.
- **Read the report in this order:** `summary` (what the numbers say, including the comparison with the betting favorite and whether the experience heuristic's edge reversed), then `win_loss.test` (`all`, `before_2026-05-17`, `from_2026-05-17`, `calibration`), then `method` and `ablation`. The favorite is compared only on test fights with odds and the coverage is printed ("241 of 553"); the paired bootstrap interval says whether a gap is more than noise. Losing to the favorite is the expected result; beating it should make you look for leakage.
- **`list`** shows versions newest first, `*` marks the active one, with the test log loss. `--json` prints every row including the full metrics.
- **`activate VERSION`** makes one version the only active one in a single transaction and prints what it deactivated. An unknown version exits 1 and changes nothing. Postgres rejects two active rows, so a half-done switch cannot happen.
- **A version's row says what it was trained on:** `data_cutoff` is the last event date the model was fit on (end of the training split); `config.dataset_last_event` is the last event date in the data the run read; `features_output_sha256` and `config.inputs_sha256` identify the features; `config` also holds the seed, splits, feature lists, parameters, library versions, git sha and each artifact's sha256.
- **Artifacts are not in git.** `data/models/` is gitignored. A lost directory can be recreated by training again with the stored seed, splits and library versions on the same features; the registry row stays valid for its metrics either way. An unwanted inactive version is removed with `DELETE FROM model_versions WHERE version = '...'` plus its directory (the active one can't be activated away without activating another, so deactivate it that way first).

## The API

The prediction service (`services/api`): `GET /fighters`, `GET /fighters/{id}`, `POST /predict`, `GET /events/upcoming`, `/healthz`, `/readyz`, `/metrics`, docs at `/docs`. Needs `docker compose up -d`, a migrated database, a features build (`features rebuild`) and an **active** model (`cageops_trainer activate`).

**Run it**

```bash
# on the host, for development
uv run --package cageops-api uvicorn cageops_api.main:app --port 8000 --no-proxy-headers --no-access-log

# in Docker: build the image, start it next to Postgres and Redis, read the models from ./data/models
docker compose --profile api up -d --build api
docker compose ps api              # "healthy" means /readyz answered 200
docker compose logs -f api         # JSON, one line per request
docker compose --profile api stop api
```

**Ready or not.** `/healthz` is 200 whenever the process answers and does no I/O. `/readyz` is 200 only with a verified model loaded and a database that answered within three polls; otherwise 503 with the reason (`no active model version`, `artifact ... does not match its recorded sha256`, `database unreachable`). Redis is never a readiness condition: the API computes uncached and counts the failure. A container is "healthy" in Docker when `/readyz` is 200.

**Switch models without a restart**

```bash
uv run python -m cageops_trainer list
uv run python -m cageops_trainer activate 20261014-225801
curl -s localhost:8000/readyz        # model_version changes within MODEL_POLL_INTERVAL_S (30 s)
```

- A version is loaded only after every file matches the checksum the registry stored and the manifest's feature names equal the code's. If it does not, the old version keeps serving, `api_model_reload_total{result="failure"}` counts it, and the log line `model_reload_failed` says why. That version is not retried for `MODEL_RELOAD_RETRY_S`.
- With no active row at startup the API starts and says not ready; it never falls back to another file.
- Deactivating every version by hand does not take a running API down: it keeps serving the verified model and logs one warning (`api_model_active_in_db` goes to 0).
- The container reads `./data/models` read-only, by version directory (`MODEL_ARTIFACT_DIR/<version>`), never the path stored in the registry.

**The cache.** `/predict` and `/events/upcoming` are cached in Redis; `X-Cache` says `hit`, `miss`, `partial` (a list with some of each) or `bypass`. The key includes the model version, the features build, a fingerprint of what the build read, the matchup, its context and today's date, so a new model, a rebuild, an ingested result and midnight (New York) each start a fresh set. To empty it by hand: `docker compose exec redis redis-cli --scan --pattern 'pred:*' | xargs -r docker compose exec -T redis redis-cli del` (`FLUSHDB` also clears the rate-limit buckets and the ingestion queue's db 0 neighbours, so prefer the pattern). Turn it off with `CACHE_ENABLED=false`.

**Rate limiting.** Per client, a token bucket in Redis; 429 with `Retry-After`. `/healthz`, `/readyz` and `/metrics` are exempt. Redis down means allowed.
- Behind a reverse proxy set `TRUSTED_PROXY_CIDRS` to the proxy's network, or every user looks like the proxy.
- The Next.js frontend sends `X-API-Key` (value `FRONTEND_API_KEY`, 32+ characters, `openssl rand -hex 32`) and the end user's address in `X-CageOps-Client-IP`. The address is believed only with a valid key. Rotate by setting the new key as `FRONTEND_API_KEY` and the old one as `FRONTEND_API_KEY_PREVIOUS`, deploy, then clear the old one.
- To see who is being limited: `api_ratelimited_total{client_class,scope}` and `api_ratelimit_ignored_headers_total{reason}` in `/metrics`.

**What to look at in `/metrics`**

| metric | tells you |
|---|---|
| `api_request_duration_seconds{route,status}` | latency by route template (p50/p95/p99 with `histogram_quantile`) |
| `api_predict_stage_seconds{stage}` | where a prediction spends its time: `db`, `assemble`, `model`, `build`, `serialize`, `cache` |
| `api_cache_hits_total` / `_misses_total` / `_bypassed_total`, `api_redis_errors_total{op}` | cache effectiveness and Redis health |
| `api_db_pool_checked_out`, `api_db_pool_checkout_seconds`, `api_db_pool_checkout_timeouts_total` | whether the connection pool is the bottleneck |
| `api_predictions_with_stats_gap_total`, `api_predictions_features_stale_total` | how often a prediction was served with incomplete rates or features |
| `api_features_inputs_match_build` | 0 means a result, bio or stat changed since the last `features rebuild` |
| `api_model_info{version}`, `api_model_reload_total{result}` | which model is serving, and reload outcomes |

**Check the built image** (CI runs this; it fails on a trainer, worker or scraper in the image, a root user, a missing healthcheck, or growth past the budget):

```bash
docker build -f infra/docker/api.Dockerfile -t cageops-api:local .
scripts/check_api_image.sh cageops-api:local
```

**Load test** (D-046; about 15 minutes for the whole matrix, and it needs the machine quiet):

```bash
docker build -f infra/docker/api.Dockerfile -t cageops-api:local .
uv run python scripts/loadtest/prepare_db.py --force     # cageops_loadtest: dev data + a synthetic card
uv run python scripts/loadtest/make_inputs.py            # unique same-division pairs from real data
uv run python scripts/loadtest/run_loadtest.py --fresh   # each run: restart API, flush, k6, scrape, validate
uv run python scripts/loadtest/run_loadtest.py --only cold_16 --duration 10s   # one run
```

Each run lands in `data/bench/api/results.jsonl` with the k6 percentiles, the server's own quantiles and stage timers, the pool statistics, and `valid` / `problems`. A run that fails its validity checks says why; do not quote it. The limiter is off except in the runs marked ON, and the API uses its own Redis database (3) so flushing it touches nothing else.

**Not ready, what to check.** `curl -s localhost:8000/readyz` names the reason. `database unreachable`: is Postgres up and `DATABASE_URL` right (inside compose the host is `postgres`)? `no active model version`: run `cageops_trainer list` and `activate`. `does not match its recorded sha256`: the files in `data/models/<version>` were changed or copied incompletely; retrain or restore them. `prediction 503 features_unavailable`: that fighter has no row in `fighter_features_current`; run `features rebuild`.

## Production

_Written in Phase 2c (VM + docker compose) and rewritten in Phase 6 (k3s)._

## Incidents

_Record what broke, how it was found, and how it was fixed._
