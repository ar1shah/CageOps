# CageOps

A production-style ML platform that predicts UFC fight outcomes. The domain is UFC; the point is the infrastructure: ingestion pipeline, job queue, model registry, serving, Kubernetes, observability.

Not affiliated with the UFC. Not betting advice.

## Status

Phase 0 (scaffold) and Phase 1a (seed data): complete and merged. Phase 1b (live ingestion): built, tested and benchmarked against a local replay server, in review; it has never run against the real ufcstats.com, which is behind a bot challenge we do not bypass (see D-013). See [docs/ROADMAP.md](docs/ROADMAP.md).

- [x] uv workspace with `cageops_common` and scraper / worker / api / trainer stubs
- [x] API with a working `/healthz`
- [x] Local Postgres 16 (pgvector) + Redis 7 via docker compose
- [x] ruff + pytest, one test per package
- [x] CI: lint, format, tests, compose validation on every PR (passing on PR #1)
- [ ] Phase 1: data pipeline
  - [x] 1a: schema, Alembic migrations, reproducible seed download with sha256 manifest, idempotent loaders, point-in-time rankings view, tests on real Postgres in CI
  - [x] 1b: live ingestion on Redis + RQ: polite fetcher with a shared rate limiter and raw-page cache, parsers, retries and a dead-letter queue, idempotent upserts, CLI, `/healthz` and `/metrics`, worker-scaling benchmark (replay server only, see [docs/BENCHMARKS.md](docs/BENCHMARKS.md))
  - [ ] 1c: point-in-time feature pipeline
- [ ] Phase 2: model, API, first deploy
- [ ] Phase 3: frontend
- [ ] Phase 4: RAG writeups
- [ ] Phase 5: retraining + grading loop
- [ ] Phase 6: Kubernetes, CI/CD, observability
- [ ] Phase 7: self-hosted inference (optional)
- [ ] Phase 8: polish

Seed data: 8,555 fights, 774 events, 2,686 fighters, 40,244 per-round stat rows, plus odds and rankings from named sources. Counts and file hashes are in [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md). Odds from the "Ultimate UFC Dataset" by Matt Dabbert are used under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) (converted from American to decimal odds and matched to fights), as a betting-favorite baseline only and never as model features.

Known gaps (intentional, scheduled later): the API has no `/metrics` endpoint yet (Phase 2b; the ingestion worker has one), no service Dockerfiles yet (Phase 2b), no live June-to-October 2026 results until the ufcstats operator lifts the bot challenge or a second source lands (Phase 1d).

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
```

More commands are in [docs/RUNBOOK.md](docs/RUNBOOK.md). Design decisions are in [docs/DECISIONS.md](docs/DECISIONS.md).
