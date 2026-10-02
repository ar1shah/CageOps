# Design Decisions

Every meaningful design decision gets an entry. Format:

```
### D-###: <title>
- Context: what problem we were solving
- Options considered: 2-3 real alternatives
- Decision: what we picked
- Tradeoff: what we gave up, and when we'd revisit
```

---

### D-001: Monorepo
- Context: CageOps has several Python services (scraper, worker, api, trainer), shared code (db models, config), a Next.js frontend, and infra manifests. We needed to decide how to lay out the repositories.
- Options considered:
  1. One repo per service, with shared code published as a package.
  2. A monorepo that uses git submodules for shared code.
  3. A single monorepo with all services, shared code, web and infra together.
- Decision: a single monorepo (option 3). Shared code lives in `packages/cageops_common` and is consumed directly by each service through the uv workspace.
- Tradeoff: one CI pipeline and atomic cross-service changes (one PR can change a shared model and every service using it), in exchange for coarser access control and CI time that grows with the repo. Revisit if separate teams or deploy cadences emerge; then path-filtered CI or splitting a service out becomes worth it.

### D-002: uv for Python packaging and workspace
- Context: we need to install Python 3.12, manage dependencies for five packages with one shared local package, and get reproducible installs in CI and Docker.
- Options considered:
  1. uv (workspaces, single lockfile, manages Python versions).
  2. Poetry (mature, but workspace support is weaker).
  3. pip + venv + pip-tools (most familiar, but no workspace concept and more manual steps).
- Decision: uv. One `uv.lock` pins every package in the workspace. Services depend on `cageops-common` via `{ workspace = true }`, and `uv sync --locked` in CI fails if the lockfile is stale.
- Tradeoff: uv is newer, with less community history than pip or Poetry, and we pin the version in CI to limit surprises. Revisit if a workspace limitation gets in the way of building per-service Docker images (Phase 2b).

### D-003: PostgreSQL 16 (not 17)
- Context: we need one Postgres major version for local dev now and production later, with the pgvector extension for embeddings in Phase 4.
- Options considered:
  1. PostgreSQL 16 with the `pgvector/pgvector` image.
  2. PostgreSQL 17.
  3. Defer the choice to whichever managed Postgres we pick later.
- Decision: PostgreSQL 16. It's what the project spec calls for, it's mature, and pgvector images for it are well tested. 17's improvements (vacuum and memory efficiency, incremental backup) don't matter at our data size.
- Tradeoff: we give up 17's newer features and a slightly longer support window. Revisit before the production deploy (Phase 2c/6), or sooner if a needed feature or pgvector release requires 17. A major upgrade is a dump/restore, which the Phase 6 backup work will exercise anyway.
