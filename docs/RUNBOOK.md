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

## Production

_Written in Phase 2c (VM + docker compose) and rewritten in Phase 6 (k3s)._

## Incidents

_Record what broke, how it was found, and how it was fixed._
