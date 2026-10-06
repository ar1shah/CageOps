# CageOps

CageOps is a production-style ML platform that predicts UFC fight outcomes. The domain is UFC; the point is the **infrastructure**. Every design choice should be one a platform / ML-infra engineer could defend in an interview.

## Who you're working with

Ari is a CS senior (UCF, graduating May 2027) with solid TypeScript / Next.js / Supabase / Postgres experience. He is **new to Python, Docker, Redis, Kubernetes, and ML serving**, and he will be interviewed on this project. That changes how you work:

- For any non-trivial task, use plan mode first: explain the approach in plain language (analogies help a lot), then wait for approval before writing code.
- Prefer the simplest design that demonstrates the infra concept. No clever abstractions he can't explain.
- When a task is done, summarize: what you built, the key tradeoff, and what could break in production.
- Log every meaningful design decision in `docs/DECISIONS.md` (format below).
- Before running any command on a remote server, show the command and say what it does.

## Architecture

```
Data sources: ufcstats.com scraper, Kaggle seed data, MMA news RSS
  -> Ingestion: Python workers consuming a Redis-backed job queue (RQ), retries + dead-letter queue
  -> Storage: PostgreSQL 16 + pgvector (raw tables, point-in-time feature tables, text chunks + embeddings)
  -> Training: scheduled retraining job (LightGBM), versioned models in a Postgres-backed registry
  -> Serving: FastAPI prediction API + Redis cache; LLM writeups via a swappable LLMClient
  -> Frontend: Next.js on Vercel
Ops: Docker everywhere, docker compose for local dev, k3s (Kubernetes) in production,
     GitHub Actions CI/CD, images on GHCR, Prometheus + Grafana, nightly Postgres backups
```

## Repo layout

```
packages/cageops_common/   shared Python code: db models, config, logging, metrics
services/scraper/          ufcstats + news fetchers and parsers
services/worker/           RQ workers that run ingestion / embedding jobs
services/api/              FastAPI prediction + writeup service
services/trainer/          training, evaluation, model registry, backtests
web/                       Next.js frontend (Vercel)
infra/docker/              Dockerfiles
infra/k8s/                 Kustomize base + overlays (local, prod)
.github/workflows/         CI/CD
docs/                      ROADMAP, DECISIONS, BENCHMARKS, RUNBOOK, DATA_SOURCES
data/                      local-only raw data (gitignored)
```

## Stack and conventions

- Python 3.12, managed with **uv** (workspace). Each service has its own pyproject and Dockerfile; shared code lives in `packages/cageops_common`.
- Lint/format with **ruff**. Type hints everywhere; **pydantic** models at every API boundary.
- DB access with **SQLAlchemy 2.0**; schema changes only through **Alembic** migrations.
- Tests with **pytest**. Parsers are tested against saved HTML fixtures, never live requests. CI must pass before merge.
- Config via environment variables only. `.env.example` documents every variable.
- Structured JSON logging. Every long-running service exposes `/healthz` and `/metrics` (Prometheus format).
- Docker images: multi-stage builds, non-root user, pinned base images, small as reasonable.

## Non-negotiable rules

1. **Point-in-time correctness.** Features for a fight may only use data from strictly before that fight's date. There must be a test that proves it.
2. **Polite scraping.** Check robots.txt, max ~1 request/second globally (shared across workers), cache raw HTML and never re-fetch a cached page unless forced, descriptive User-Agent.
3. **No secrets** in code, logs, commits, Docker images, or CI output.
4. **Honest evaluation.** Never a random train/test split — time-based only. Always report against baselines (the betting favorite where odds exist, and a simple experience/ranking heuristic). Report log loss and calibration, not just accuracy. Never frame the product as a betting tool.
5. **Measure it.** Any performance claim goes in `docs/BENCHMARKS.md` with the exact command, hardware, and date that produced it.
6. **Git hygiene.** Feature branch per task, small commits with clear messages, one PR per phase (or sub-phase).
7. **Stay in phase.** Only work on the current phase in `docs/ROADMAP.md` unless Ari asks otherwise.

## Commands

(Keep this section updated as things are built.)

- `cp -n .env.example .env` — create local env file (never overwrites)
- `docker compose up -d` — start Postgres (pgvector) + Redis locally
- `docker compose down` — stop them (data kept); `docker compose down -v` also deletes local data
- `uv sync --all-packages --all-groups` — install deps for every workspace package
- `uv run pytest` — run tests (DB tests need `docker compose up -d`; set `REQUIRE_DB=1` to fail instead of skip)
- `uv run alembic -c packages/cageops_common/alembic.ini upgrade head` — apply migrations (`check` detects model/migration drift)
- `uv run python -m cageops_worker.seed` — load seed data into Postgres (idempotent; verifies the manifest first)
- `uv run ruff check . && uv run ruff format .` — lint + format
- `scripts/download_seed.sh [--force]` — download seed data to `data/raw/` and write the sha256 manifest (needs `~/.kaggle/` credentials)
- `uv run --package cageops-api uvicorn cageops_api.main:app --port 8000` — run the API locally
- `uv run python scripts/minimize_fixture.py data/fixtures/ufcstats/*.html --out services/scraper/tests/fixtures/ufcstats` — shrink hand-saved ufcstats pages into committable fixtures (deletes only; full pages stay in gitignored `data/fixtures/`, D-017)
- `uv run python -m cageops_scraper.parsers services/scraper/tests/fixtures/ufcstats/fight_32054bf2b36b0e47.html` — print what a parser makes of a saved page, as JSON (file only, no network; kind and URL come from the file name)
- `uv run python -m cageops_worker.ingest backfill --since YYYY-MM-DD [--force]` — queue every completed event since a date (or `--months N`); prints a run id. `--force` re-fetches cached pages. Refuses (exit 3) while the circuit breaker is open
- `uv run python -m cageops_worker.ingest scrape-upcoming [--force]` — queue a read of the upcoming events
- `uv run python -m cageops_worker.ingest worker [--burst] [--max-jobs N] [--metrics-port P]` — run one ingestion worker (Linux/macOS/WSL2; serves `/healthz` and `/metrics` on 9100; holds, never exits, while the site blocks us)
- `uv run python -m cageops_worker.ingest status [--run RUN_ID] [--json]` — queue depth, dead letters, breaker; with `--run`, rows inserted/updated/unchanged
- `uv run python -m cageops_worker.ingest dlq list [--reason R] | inspect <job_id> | replay <job_id> | replay --all [--reason R] | purge <job_id>` — the dead-letter queue
- `uv run python -m cageops_worker.ingest breaker status | reset --yes` — see or clear the circuit breaker (check the site yourself first)
- `uv run python scripts/standin_site.py` — a local stand-in for ufcstats.com that serves the saved fixtures (modes: ok / fail / challenge) for trying ingestion with no real traffic; walkthrough in `docs/RUNBOOK.md`
- `uv run python scripts/standin_site.py --synthetic-events 52 --latency-ms 250` — the same server as a replay server: a generated year of weekly events, simulated response time (`--jitter`, `--seed`, `--corpus-end`, `--quiet`). Replay numbers are never real-site numbers
- `uv run python scripts/bench_ingest.py run --workers 4 --interval-ms 100 --latency-ms 250 [--mode cold|warm|rerun]` — one ingestion benchmark run against the replay server (own `cageops_bench` database, Redis db 2); `matrix` runs the whole planned set (about an hour). Results go to `data/bench/results.jsonl`, the committed numbers to `docs/BENCHMARKS.md`

## DECISIONS.md entry format

```
### D-###: <title>
- Context: what problem we were solving
- Options considered: 2-3 real alternatives
- Decision: what we picked
- Tradeoff: what we gave up, and when we'd revisit
```

## Skills in this repo

- `/explain` — teach Ari what was just built and quiz him on it. Offer to run it after finishing any task.
- `/phase-done` — verify the current phase's definition of done before opening a PR.
