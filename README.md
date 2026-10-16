# CageOps

A production-style ML platform that predicts UFC fight outcomes. The domain is UFC; the point is the infrastructure: ingestion pipeline, job queue, model registry, serving, Kubernetes, observability.

Not affiliated with the UFC. Not betting advice.

## Status

Phase 0 (scaffold) and Phase 1 (data pipeline: 1a seed data, 1b live ingestion, 1c point-in-time features, 1d a results source that permits automated access): complete and merged. 1b was tested and benchmarked against a local replay server only; it has never run against the real ufcstats.com, which is behind a bot challenge we do not bypass (see D-013). English Wikipedia's event articles fill the 2026-05-17 to 2026-10-03 gap (18 events, 224 fights, results only, no per-round stats; D-028, D-029). See [docs/ROADMAP.md](docs/ROADMAP.md).

- [x] uv workspace with `cageops_common` and scraper / worker / api / trainer stubs
- [x] API with a working `/healthz` (grown in 2b-1; see below)
- [x] Local Postgres 16 (pgvector) + Redis 7 via docker compose
- [x] ruff + pytest, one test per package
- [x] CI: lint, format, tests, compose validation on every PR (passing on PR #1)
- [x] Phase 1: data pipeline
  - [x] 1a: schema, Alembic migrations, reproducible seed download with sha256 manifest, idempotent loaders, point-in-time rankings view, tests on real Postgres in CI
  - [x] 1b: live ingestion on Redis + RQ: polite fetcher with a shared rate limiter and raw-page cache, parsers, retries and a dead-letter queue, idempotent upserts, CLI, `/healthz` and `/metrics`, worker-scaling benchmark (replay server only, see [docs/BENCHMARKS.md](docs/BENCHMARKS.md))
  - [x] 1c: point-in-time feature pipeline: `fight_features` (career / last-3 / last-5 rates, streaks, bio), built only from fights strictly before each fight, with leakage tests; rebuild with one command (D-027)
  - [x] 1d: a second results source: English Wikipedia event articles (`/wiki/` pages only, robots-compliant), per-source keys and `result_source` so ufcstats always wins, spelling-variant names held for review; the summer 2026 gap loaded with results only (D-025, D-028, D-029)
- [ ] Phase 2: model, API, first deploy
  - [x] 2a: LightGBM win/loss and method models on time-based splits, scored against the betting favorite and an experience/ranking heuristic (the model **loses to the favorite**, see [docs/BENCHMARKS.md](docs/BENCHMARKS.md)), versions in a Postgres registry with one active version enforced by the database, training refuses stale features (D-031, D-032, D-033)
  - [ ] 2b: prediction API, in two PRs
    - [x] 2b-1: shared inference module (training and serving build one model input; the API image carries no training code), `fighter_features_current` and the date-relative features derived for the date asked about, a model manager that verifies the active bundle and hot-reloads by polling, `GET /fighters`, `GET /fighters/{id}`, `POST /predict` with top contributing features and a `data_quality` block that keeps "features current" apart from "rates complete", `/healthz`, `/readyz`, `/metrics` (D-035 to D-041)
    - [ ] 2b-2: the serving layer
      - [x] Redis prediction cache keyed by model, features build, input fingerprint, matchup, context and date, flipped exactly for B-vs-A, failing open (D-042)
      - [x] per-client rate limiting: token bucket in Redis, trusted-proxy rule, server-to-server key plus forwarded user IP for the frontend (D-043)
      - [x] `GET /events/upcoming`, batched; title and rounds stay NULL because the upcoming page has no round count (D-044)
      - [x] multi-stage API image (612 MiB), compose `api` service, CI builds and checks the image (D-045)
      - [x] load test (k6, D-046): cold 219 req/s, warm 1,364 req/s, mixed 382 req/s on one process; the process, not the database pool, is the limit; the limiter, once its Redis call stopped hopping threads, costs 0.10 ms per request and 14% of cached throughput (BENCHMARKS.md)
  - [ ] 2c: first deploy
- [ ] Phase 3: frontend
- [ ] Phase 4: RAG writeups
- [ ] Phase 5: retraining + grading loop
- [ ] Phase 6: Kubernetes, CI/CD, observability
- [ ] Phase 7: self-hosted inference (optional)
- [ ] Phase 8: polish

Seed data: 8,555 fights, 774 events, 2,686 fighters, 40,244 per-round stat rows, plus odds and rankings from named sources. Counts and file hashes are in [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md). Odds from the "Ultimate UFC Dataset" by Matt Dabbert are used under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) (converted from American to decimal odds and matched to fights), as a betting-favorite baseline only and never as model features.

Known gaps (intentional, scheduled later): one process is the throughput limit (about 220 cold and 1,500 cached req/s on this machine; fewer queries per prediction and more replicas are the follow-ups, BENCHMARKS.md), the image is built in CI but not pushed anywhere (2c, Phase 6), the Redis in compose has no `maxmemory` (2c), the database holds no scheduled bouts because the only source of upcoming cards is ufcstats (D-025) so `/events/upcoming` will be empty on real data, rebuilding `fight_features` / `fighter_features_current` is not scheduled so `data_quality.features_current` is false for anyone whose newest result was ingested after the last build (Phase 5), `is_title_fight` and `scheduled_rounds` are not model features until upcoming bouts get them (D-032), the betting-favorite comparison covers only 241 of the 553 test fights and none of the 223 from 2026-05-17, because mdabbert odds end 2026-03-28 (together with the jerzyszocik daily odds dataset 478 would be covered, including 162 of those 223; that loader is deferred to Phase 5, D-034), no per-round stats for fights after 2026-05-16 until the ufcstats operator can let us past the bot challenge (D-025); those fights come from Wikipedia and count in a fighter's history but not in the rate features. If ufcstats access returns, an adoption step is still needed to merge Wikipedia-created rows with ufcstats' (D-029).

## Quickstart

```bash
cp -n .env.example .env
docker compose up -d
uv sync --all-packages --all-groups
uv run pytest

# load the seed data (needs Kaggle credentials in ~/.kaggle/)
scripts/download_seed.sh
uv run alembic -c packages/cageops_common/alembic.ini upgrade head
uv run python -m cageops_worker.seed

# build features, train, activate a model, and ask it something
uv run python -m cageops_worker.features rebuild
uv run python -m cageops_trainer train
uv run python -m cageops_trainer activate <version from `list`>
uv run --package cageops-api uvicorn cageops_api.main:app --port 8000 --no-proxy-headers --no-access-log
curl -s localhost:8000/readyz
curl -s -X POST localhost:8000/predict -H 'content-type: application/json' \
  -d '{"fighter_a_id": 194, "fighter_b_id": 147}'   # ids from GET /fighters?search=
```

More commands are in [docs/RUNBOOK.md](docs/RUNBOOK.md). Design decisions are in [docs/DECISIONS.md](docs/DECISIONS.md).

## Licences and attribution

Results for fights dated 2026-05-17 and later come from [English Wikipedia](https://en.wikipedia.org/), whose text is licensed [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/). We store facts only (names, dates, winners, methods, rounds and times) and credit each event's article by URL (see [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md) section 6). **The fixtures directory `services/scraper/tests/fixtures/wikipedia/` holds trimmed copies of Wikipedia articles and is CC BY-SA 4.0, not covered by this repository's licence**; its NOTICE lists the articles, revisions and changes. The cached HTML in `raw_pages` is the same text and carries the same licence.
