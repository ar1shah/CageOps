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

### D-004: ufcstats.com access policy
- Context: the data source audit (docs/DATA_SOURCES.md) couldn't retrieve robots.txt. A manual check on 2026-10-01 found that `http://ufcstats.com/robots.txt` returns 404 (no crawl rules published), and the site footer has no terms of use or any language about scraping.
- Options considered:
  1. Scrape with no safeguards, since no rules exist.
  2. Scrape politely anyway.
  3. Don't scrape; rely only on Kaggle snapshots.
- Decision: scrape politely anyway (option 2). The limits are ~1 request/second globally, shared across all workers; a raw HTML cache so pages are never re-fetched unless forced; and a descriptive User-Agent with a contact email. The scraper fetches robots.txt on startup and treats a 404 as "allowed", but would honor any rules or Crawl-delay that appear later.
- Tradeoff: the absence of terms is not permission, so some legal and ethical risk remains. A slower backfill is the cost of being polite. Revisit if the site adds terms or robots rules, or if we get contacted.

### D-005: Drop the ESPN MMA API as a data source
- Context: the audit found that the Disney Terms of Use explicitly prohibit automated extraction and use of content for AI/ML training, testing, or benchmarking, and limit use to personal, noncommercial. The endpoints are also undocumented and unstable.
- Options considered:
  1. Use ESPN anyway.
  2. Use it for the non-ML parts only.
  3. Drop it.
- Decision: drop it (option 3). Everything it offered (results, stats, odds, rankings) is covered by ufcstats plus the Kaggle datasets. "ESPN MMA API" is removed from the architecture diagram in CLAUDE.md.
- Tradeoff: we lose a redundant source and a possible live-odds feed. Revisit only if ESPN publishes an official API with permissive terms.

### D-006: Seed data strategy
- Context: several Kaggle UFC datasets exist, and some ship precomputed rolling features that we can't prove are point-in-time correct (CLAUDE.md rule 1).
- Options considered:
  1. One all-in-one dataset with precomputed features.
  2. The mdabbert dataset for everything.
  3. Raw facts from the jerzyszocik "silver" file, plus separate odds and rankings sources.
- Decision: option 3. Stats come from jerzyszocik silver (raw per-round facts, the same shape the scraper produces). Odds come from mdabbert (CC BY 4.0), used as a baseline only and never as model features. Rankings come from the jerzyszocik rankings history. The "golden" precomputed features are NOT loaded.
- Tradeoff: more loaders and a join across sources, in exchange for a single raw schema shared by seed and scraped data and a leakage test that actually means something. Revisit if silver turns out to lack per-round columns or has bad date coverage.

### D-007: Reproducible seed downloads with a sha256 manifest
- Context: the seed datasets are Kaggle files that their authors refresh (rankings weekly, odds daily). Kaggle's CLI can't download an older version of a dataset and doesn't expose a version number, so "the data we loaded" can't be re-fetched later and row counts could silently change between pulls.
- Options considered:
  1. Download by hand once and document it in prose.
  2. Script the download and record Kaggle's version or last-updated date.
  3. Script the download and fingerprint every file we actually use with a sha256 manifest.
- Decision: option 3. `scripts/download_seed.sh` fetches only the files we load (listed in `scripts/seed_sources.json`), verifies each one actually landed (unzipping if Kaggle sent a `.zip`), and `scripts/write_manifest.py` writes `data/raw/MANIFEST.json` with size, sha256, download time, Kaggle's file creation timestamp, and license. A copy is committed as `docs/seed_manifest.json` (hashes and dates only, no data) so any row count in the docs can be tied to exact input bytes. Existing files are always re-hashed, `--force` re-downloads, and a missing file is an error, so the manifest can't describe something that isn't there.
- Tradeoff: we can detect that the data changed but can't recover the old data (Kaggle won't serve it), so reproducing an old result means keeping a local copy of `data/raw/`. The Kaggle CLI is pinned (2.2.4) because its behavior has changed between releases. Revisit if we need true archival: then store the raw files in our own object storage keyed by sha256.
