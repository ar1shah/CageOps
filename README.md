# CageOps

A production-style ML platform that predicts UFC fight outcomes. The domain is UFC; the point is the infrastructure: ingestion pipeline, job queue, model registry, serving, Kubernetes, observability.

Not affiliated with the UFC. Not betting advice.

## Status

Phase 0 (scaffold): complete, pending merge. See [docs/ROADMAP.md](docs/ROADMAP.md).

- [x] uv workspace with `cageops_common` and scraper / worker / api / trainer stubs
- [x] API with a working `/healthz`
- [x] Local Postgres 16 (pgvector) + Redis 7 via docker compose
- [x] ruff + pytest, one test per package
- [x] CI: lint, format, tests, compose validation on every PR (passing on PR #1)
- [ ] Phase 1: data pipeline
- [ ] Phase 2: model, API, first deploy
- [ ] Phase 3: frontend
- [ ] Phase 4: RAG writeups
- [ ] Phase 5: retraining + grading loop
- [ ] Phase 6: Kubernetes, CI/CD, observability
- [ ] Phase 7: self-hosted inference (optional)
- [ ] Phase 8: polish

Known gaps (intentional, scheduled later): no `/metrics` endpoint yet (Phase 2b), no structured logging yet (Phase 1), no service Dockerfiles yet (Phase 2b).

## Quickstart

```bash
cp -n .env.example .env
docker compose up -d
uv sync --all-packages --all-groups
uv run pytest
```

More commands are in [docs/RUNBOOK.md](docs/RUNBOOK.md). Design decisions are in [docs/DECISIONS.md](docs/DECISIONS.md).
