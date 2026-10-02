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

The loader checks every input file's sha256 against `docs/seed_manifest.json` and refuses to
run on a mismatch. It prints a JSON report per source: rows read / loaded / rejected, what
didn't match and why. Every run is also recorded in the `load_runs` table with the file hash.

Tests that touch the database use a separate `cageops_test` database on the same server and
skip with a reason if Postgres isn't running. To make an unreachable database a failure
(like CI does), run `REQUIRE_DB=1 uv run pytest`.

## Production

_Written in Phase 2c (VM + docker compose) and rewritten in Phase 6 (k3s)._

## Incidents

_Record what broke, how it was found, and how it was fixed._
