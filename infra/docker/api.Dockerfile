# syntax=docker/dockerfile:1
#
# The CageOps prediction API (Phase 2b). Build from the repository root:
#
#   docker build -f infra/docker/api.Dockerfile -t cageops-api:local .
#
# What is in the image: the API, cageops_common, and the dependencies they pin in uv.lock
# (LightGBM and numpy through the `inference` extra, plus libgomp1, which LightGBM links against).
# What is NOT: the trainer, the worker, the scraper, tests, uv, a compiler, any model, any .env.
# Models are mounted at /models (read-only) so a retrain is a new directory, not a new image, and
# hot reload (D-036) means something.
#
# Base images are pinned by tag AND digest. To move them: pull the new tag, read its digest with
# `docker inspect --format '{{index .RepoDigests 0}}'`, change both lines, rebuild.

ARG PYTHON_IMAGE=python:3.12.15-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.21@sha256:a7aed3216253ee804de3e2d8afa5073baa1a177335345d43845cd4165e43b711

# ---- builder: resolve and install the locked dependencies into a virtualenv ---------------------
FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS builder
COPY --from=uv /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /src
# Dependency layer first: it only changes when the lock or a pyproject does, so a code edit does
# not reinstall LightGBM.
COPY pyproject.toml uv.lock .python-version ./
COPY packages/cageops_common/pyproject.toml packages/cageops_common/pyproject.toml
COPY services/api/pyproject.toml services/api/pyproject.toml
# The workspace names every member; the ones this image does not ship need only exist as manifests
# for uv to resolve the workspace. Their source is never copied.
COPY services/scraper/pyproject.toml services/scraper/pyproject.toml
COPY services/worker/pyproject.toml services/worker/pyproject.toml
COPY services/trainer/pyproject.toml services/trainer/pyproject.toml

COPY packages/cageops_common packages/cageops_common
COPY services/api services/api
RUN uv sync --frozen --no-dev --no-editable --package cageops-api

# ---- runtime ------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

# libgomp1: the OpenMP runtime LightGBM's wheel links against; slim images do not have it.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 \
 && rm -rf /var/lib/apt/lists/*

# A fixed, unprivileged identity. The numeric id matters for orchestrators that refuse a non-numeric
# USER, and for read-only mounts whose owner is on the host.
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin app

COPY --from=builder /app/.venv /app/.venv

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MODEL_ARTIFACT_DIR=/models

USER 10001:10001
EXPOSE 8000

# Readiness, not liveness: "healthy" means a verified model is loaded and the database answers.
# (python's urlopen raises on the 503, so a not-ready API is unhealthy.) No curl in the image.
HEALTHCHECK --interval=15s --timeout=4s --start-period=25s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=3)"]

# One process per container: scale with replicas, not workers (no prometheus multiprocess mode).
# --no-proxy-headers: the API decides whom to trust itself (D-043). --no-access-log: it logs
# requests itself, as JSON.
CMD ["uvicorn", "cageops_api.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--no-proxy-headers", "--no-access-log"]
