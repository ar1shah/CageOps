# CageOps Roadmap

Each phase has three parts: **what only you can do**, **the prompt to paste into Claude Code**, and **done when**.

**The loop for every task:**
plan mode (Shift+Tab) → read the plan, push back, approve → Claude builds → tests pass → `/explain` → commit.
**End of every phase:** `/phase-done` → PR → merge to main.

**Pace:** ~14 weeks at 8–10 hrs/week → finished around early January, in time for spring applications.
**Start each session** by running `claude` in the repo root. It reads CLAUDE.md automatically. Tell it which phase you're on.

---

## Phase 0 — Scaffold (week 1)

### You do
- Install: Git, Docker Desktop, uv, Node 20+, kubectl, k3d, GitHub CLI (`gh`), Claude Code.
- Create a **public** GitHub repo named `cageops`, clone it, copy in this starter kit (CLAUDE.md, docs/, .claude/). Commit and push it yourself.

### Prompt
```
Read CLAUDE.md and docs/ROADMAP.md. We're starting Phase 0. Use plan mode and show me the full scaffold before creating anything.

Set up:
- a uv workspace with packages/cageops_common and services/{scraper,worker,api,trainer} as minimal stubs (the api gets a working /healthz)
- web/ as an empty placeholder (don't generate the Next.js app yet)
- infra/docker and infra/k8s directories
- docker-compose.yml running Postgres 16 with pgvector and Redis 7, with named volumes and healthchecks
- .env.example, .gitignore (must block .env files and data/)
- ruff + pytest config and one passing test per package
- a GitHub Actions workflow that runs lint and tests on every pull request
- docs/DECISIONS.md, docs/BENCHMARKS.md, docs/RUNBOOK.md templates, and a README with a "Status" section

Log D-001 (monorepo) and D-002 (uv) in DECISIONS.md. When you're done, walk me through every piece and how to run it, like I've never used Docker or uv.
```

### Done when
- [ ] `docker compose up -d` starts Postgres (pgvector) and Redis, both healthy
- [ ] `uv run pytest` and ruff pass locally and in CI on a PR
- [ ] DECISIONS.md has D-001 and D-002

---

## Phase 1 — Data pipeline (weeks 2–4)

### You do
- Run **Cowork prompt 1** (data source audit) first. Save the output as `docs/DATA_SOURCES.md`.
- Make a Kaggle account and generate an API token (`~/.kaggle/kaggle.json`), or download the chosen dataset manually into `data/raw/`.
- Sanity-check the loaded data against fights you actually know. You're the domain expert here.

### Prompt 1a — historical seed
```
Phase 1a: historical seed data. Read docs/DATA_SOURCES.md. The seed dataset is in data/raw/ (or use the kaggle CLI — my token is configured).

Plan first: inspect the files, tell me what columns exist and what data-quality problems you see, and propose a normalized Postgres schema (fighters, events, fights, per-fighter fight stats, odds, rankings). Then build Alembic migrations, an idempotent loader (safe to rerun without duplicating), and tests. Report row counts and anything you dropped and why. Log schema decisions in DECISIONS.md.
```

### Prompt 1b — live scraper + job queue
```
Phase 1b: live ingestion from ufcstats.com using Redis + RQ.

Design: a scheduler enqueues "discover events" jobs → workers parse event pages and enqueue "fetch fight" and "fetch fighter" jobs → results upsert into the same tables as the seed data.

Requirements:
- check robots.txt first and tell me what it says before writing the scraper
- a global rate limit of ~1 request/second shared across ALL workers, implemented in Redis — explain how it works
- cache raw HTML in a raw_pages table (url, fetched_at, status, html); never re-fetch cached pages unless forced
- retries with exponential backoff; jobs that keep failing go to a dead-letter queue I can inspect and replay
- idempotent upserts
- parsers unit-tested against saved HTML fixtures, not live requests
- CLI commands: backfill since <date>, scrape upcoming events, inspect/replay dead-letter queue
- Prometheus metrics: jobs processed/failed, queue depth, fetch latency

Then benchmark a 12-month backfill with 1 worker vs 4 workers and log it in BENCHMARKS.md. Explain why 4 workers isn't 4x faster here.
```

### Prompt 1c — point-in-time features
```
Phase 1c: point-in-time feature pipeline. Before coding, explain data leakage to me with an analogy and show a concrete example of how it would sneak into this project.

For each fight, compute each fighter's features using ONLY fights strictly before that fight's date: career and last-3/last-5 averages for significant strikes landed and absorbed per minute, striking accuracy and defense, takedowns landed and defended per 15 min, submission attempts, knockdowns, total fights, win streak, finish rate, days since last fight, age at fight date, height and reach, stance, weight class.

Store in a fight_features table keyed by (fight_id, fighter_id). Write a test that proves there's no leakage — e.g., change a future fight's stats and assert past features don't change. Make it rebuildable from scratch with one command.
```

### Phase 1d — a results source that permits automated access (added 2026-10-03, do before Phase 5)
ufcstats.com put every content page behind a bot challenge on 2026-10-03, and we don't bypass it (D-013). That leaves a gap from June 2026 (where the Kaggle seed stops) to today, and Phase 5 needs fresh results to grade predictions.
- Research first: the Wikipedia / MediaWiki API terms (rate limits, User-Agent policy, CC BY-SA attribution) and any alternatives; log what was checked and when in DATA_SOURCES.md and DECISIONS.md.
- Add it as a new `Source` in the Phase 1b fetcher (same queue, cache, rate limiter, DLQ). Expect results, method, round and time, but probably no per-round stats; store what it has and leave stats NULL.
- If the ufcstats operator grants access in the meantime, re-run the 1b backfill against the live site instead.
- **Built 2026-10-06:** English Wikipedia event articles (D-028), stored with per-source keys and per-field precedence (D-029); the operator replied that scraping is fine but they cannot control the challenge (D-025). The gap (18 events, 224 fights) is loaded. Still open: an allowlist or export from the operator, and the step that merges Wikipedia-created rows with ufcstats' if access returns.

### Done when
- [ ] Seed data loaded; row counts documented; loader reruns cleanly
- [ ] Scraper backfills 12 months idempotently; dead-letter queue works
- [ ] Leakage test passes
- [ ] Worker scaling benchmark in BENCHMARKS.md

---

## Phase 2 — Model, API, first deploy (weeks 5–6)

### You do
- Run **Cowork prompt 2** (hosting + cost sheet) and pick a host.
- Create the cloud account (check the GitHub Student Developer Pack for credits first), create the VM, add your SSH key.
- Add a DNS record: `api.cageops.ar13.dev` → the VM's IP.
- Look at predictions for upcoming fights and tell Claude which ones look insane.

### Prompt 2a — model + registry
```
Phase 2a: model training and registry. Keep the modeling simple — this is a systems project.

- LightGBM win/loss model on matchup features (differences between the two fighters), plus a method-of-victory model (KO/TKO, submission, decision)
- time-based splits only: train on older fights, validate on the next year, test on the most recent year. Never random.
- randomly swap fighter order during training to remove corner bias — explain why that matters
- report accuracy, log loss, Brier score, and a calibration check, compared against two baselines: the betting favorite (where odds exist) and a simple experience/ranking heuristic
- a Postgres-backed model registry: model_versions (version, trained_at, data_cutoff, metrics JSON, artifact_path, is_active)
- a training CLI that registers a new version

Tell me honestly how the model compares to the betting-favorite baseline. Don't oversell it.
```

### Prompt 2b — prediction API
```
Phase 2b: FastAPI prediction service.

Endpoints: GET /fighters?search=, GET /fighters/{id}, POST /predict {fighter_a_id, fighter_b_id} returning win probabilities, method probabilities, model_version, and top contributing features; GET /events/upcoming with predictions for every bout.

- load the active model at startup; hot-reload when the active version changes
- cache predictions in Redis keyed by (fighter_a, fighter_b, model_version) — explain why model_version has to be in the key
- pydantic schemas, OpenAPI docs at /docs, input validation, per-IP rate limiting
- /healthz, and /metrics with a request latency histogram and cache hit/miss counters
- multi-stage Dockerfile, non-root user

Then load-test with locust or k6: record p50/p95/p99 latency and requests/sec with a cold vs warm cache in BENCHMARKS.md.
```

**Carry-over from 2a (read before starting 2b):**
- Load the active model from `model_versions` (`is_active`), verify each artifact against `config.artifacts_sha256`, and log `features_output_sha256` at startup. Predict with `WinLossModel.predict` (it averages both fighter orderings, so P(A) + P(B) = 1) and `MethodModel.predict`; the feature vector for a fighter is the same 34 numeric columns plus normalized stance and weight class (`cageops_trainer.features`, and `manifest.json` in the artifact directory).
- Upcoming bouts: fill `is_title_fight` and `scheduled_rounds` from the upcoming-event data (title fights and main events are known in advance) and write them into `fight_features` for scheduled fights, so Phase 5 can bring them back as features (D-032; measured cost of leaving them out: 0.006 val log loss on the method model).
  - **Not done, on evidence (2026-10-15, D-044).** An upcoming fight-details page carries the title marker (bout heading and belt image) but no round count: "Time format" and the whole result block are absent until the fight happens. So `scheduled_rounds` has no pre-fight source on ufcstats, and the scheduled-fight job was not built. `is_title_fight` and `scheduled_rounds` stay NULL for scheduled bouts. A title-only parser is a known option if a rounds source ever appears.
- The training staleness guard ignores scheduled bouts, so 2b needs its own check that the features for an upcoming fight are current before predicting it (D-033).
- The image needs `apt-get install libgomp1` (LightGBM), and the API must not pull in the training code: move the small inference pieces to a shared module rather than importing `cageops_trainer` into the API image.
- **Rate features are stale for anyone who fought on or after 2026-05-17.** Those fights are Wikipedia results with no per-round stats (D-025, D-029), so a fighter's rates exclude them, and nothing can fill them in while ufcstats is gated. 406 fighters were affected on 2026-10-14: `SELECT count(DISTINCT p) FROM (SELECT fighter_a_id AS p FROM fights f JOIN events e ON e.id = f.event_id WHERE e.event_date >= '2026-05-17' AND f.status = 'completed' UNION SELECT fighter_b_id FROM fights f JOIN events e ON e.id = f.event_id WHERE e.event_date >= '2026-05-17' AND f.status = 'completed') x`. So the upcoming-bout freshness check must tell "features current" from "rates complete": compare `n_fights_career`, `n_fights_last3` and `n_fights_last5` with `n_fights_with_stats_*` for each fighter (both exist in `fight_features`; the models do not use them, D-032) and return the gap (for example `stats_gap_fights`) to the caller rather than failing the prediction or calling it fresh. The only measured sign of the cost is the Wikipedia-period test slice (223 fights, log loss 0.6878 against 0.6566 before it, BENCHMARKS.md).

### Prompt 2c — first real deploy
```
Phase 2c: first production deploy on a plain VM with docker compose. (We'll migrate to Kubernetes in Phase 6 — doing it in this order is intentional.)

I've created a VM (Ubuntu) and can SSH in. Walk me through: creating a non-root user, SSH-key-only login, a ufw firewall, installing Docker, then deploying Postgres, Redis, the API, and workers with a production compose file and Caddy for automatic HTTPS on api.cageops.ar13.dev.

Show me every command before running it. Write the whole process as a runbook in docs/RUNBOOK.md so I could redo it without you.
```

### Done when
- [x] Model metrics vs both baselines documented honestly (2a: docs/BENCHMARKS.md; the model loses to the favorite)
- [ ] `https://api.cageops.ar13.dev/docs` is live
- [ ] Load-test numbers (cold and warm cache) in BENCHMARKS.md
- [ ] RUNBOOK.md can rebuild the server from scratch

---

## Phase 3 — Frontend (week 7)

### You do
- You already know Next.js. Consider building a good chunk of this yourself and having Claude review your PRs — it's the one phase where you're faster than the learning curve.
- Connect the `web/` folder to Vercel, set the API URL env var, add DNS for `cageops.ar13.dev`.

### Prompt
```
Phase 3: frontend in web/ — Next.js (App Router, TypeScript, Tailwind), deployed on Vercel.

Pages: home with fighter search and a two-fighter matchup builder; matchup result page (win probability, method breakdown, key factors, model version); fighter profile with stat trends (Recharts); upcoming event page with predictions for every bout; /model page for performance over time (placeholder until Phase 5); /about explaining the architecture with the diagram.

Server components calling the API via an API_URL env var. Dark theme, mobile-friendly, feels like a real product. Footer disclaimer: not affiliated with the UFC, not betting advice.
```

### Done when
- [ ] `cageops.ar13.dev` is live and a stranger could predict any two fighters with no instructions

---

## Phase 4 — RAG matchup writeups (weeks 8–9)

### You do
- Create an Anthropic Console account, generate an API key, and **set a monthly spend limit**.
- Pick 20 matchups you know well for the eval set; write them into `docs/eval_matchups.md`.

### Prompt
```
Phase 4: RAG writeups for matchups.

Ingestion (worker jobs): pull the news RSS feeds listed in docs/DATA_SOURCES.md, extract article text, dedupe, chunk (~500 tokens with overlap), tag chunks with fighter IDs via name matching, embed with an open-source embedding model running on CPU in the worker (pick one, justify it in DECISIONS.md), store in pgvector with an HNSW index.

Retrieval: for a matchup, filter to chunks tagged with either fighter, rank by vector similarity plus recency.

Generation: POST /writeup sends the prediction output plus retrieved chunks to the Anthropic API (claude-haiku-5-5), instructing it to use only the provided sources and cite them; the response includes source URLs. Cache writeups in Redis.

Put the LLM call behind an LLMClient interface so a self-hosted vLLM server can be swapped in later without touching the endpoint.

Build an eval script over the 20 matchups in docs/eval_matchups.md that checks every cited claim maps to a retrieved chunk. User-facing output shows only short excerpts and links, never full article text.

Explain RAG to me with an analogy before building it.
```

### Done when
- [ ] Writeups cite real sources; eval script results logged
- [ ] Writeups cached; cost per writeup estimated in BENCHMARKS.md

---

## Phase 5 — Closed loop: retraining + grading (week 10)

### Prompt
```
Phase 5: close the loop.

- store every prediction at the time it's made in a predictions table (with model_version and timestamp)
- scheduled jobs: after each event, scrape results and grade every pre-event prediction
- weekly retrain; promote the new model to active ONLY if it beats the current one on the latest holdout (log loss). Otherwise keep the old one and log why. Explain this promotion gate to me with an analogy.
- /model/performance endpoint: accuracy, log loss, and calibration by model version and over time; wire it into the /model page
- a backtest CLI that replays a past date range as if live
```

### Carry-over from the 2a gap investigation (read before starting Phase 5)
- **Results after an event come from Wikipedia while ufcstats is gated** (D-013, D-025, D-028). Grading and the weekly forced refresh below must therefore run against that source: `SCRAPER_SOURCE=wikipedia INGEST_QUEUE=ingest-wikipedia` (RUNBOOK, "Wikipedia results"), and `backfill --since <date> --force` re-fetches articles older than the 30-day window. They carry results only, no per-round stats, so retraining keeps excluding those fights from the rate features (D-027). If the operator allowlists our User-Agent or shares an export, the adoption step that merges Wikipedia-created rows with ufcstats' (D-029) comes first.
- **`is_title_fight` and `scheduled_rounds` cannot come back as model features yet (D-044).** Completed fights have them, but no source gives them for an upcoming bout (the upcoming page has a title marker and no round count), so a model trained with them would be fed NULL at serving time: the train/serve difference D-027 warned about. Do not add them to the feature set until a source supplies both columns before the fight. The measured cost of leaving them out is still 0.006 validation log loss on the method model (D-032).
- **The daily odds loader (D-034).** Grading real events needs prices for events mdabbert will never cover, and the favorite comparison needs them for the Wikipedia period (0 of 223 fights today; 162 with the jerzyszocik daily dataset). Scope, measurements and the baseline-policy question are in D-034 and BENCHMARKS.md; re-download and re-run `scripts/odds_daily_probe.py` first, because the file changes daily.

### Done when
- [ ] Predictions for at least one real event were stored beforehand and graded after
- [ ] Promotion gate tested both ways (promote and reject)
- [ ] /model page shows real data
- [ ] Weekly forced refresh is scheduled and has run at least once: `backfill --since <today - 6 months> --force` (about 300 requests, about 5 minutes at 1 request/second). Without it, results overturned after the 3-day cache window (win to no contest) stay wrong in `fights`, and Phase 5 would grade against the old result. Grading must re-grade a prediction when a `result_changed` anomaly fires. See the D-015 amendment (2026-10-03) in DECISIONS.md.

---

## Phase 6 — Kubernetes, CI/CD, observability (weeks 11–12)

### You do
- Add GitHub Actions secrets: kubeconfig for the cluster, GHCR credentials.
- Create an object storage bucket + keys for backups.
- Resize the VM if Claude says memory is too tight.

### Prompt
```
Phase 6: move production from docker compose to Kubernetes (k3s). Explain each Kubernetes object to me before writing its YAML.

1. Locally with k3d: Kustomize manifests (base + overlays/local + overlays/prod) for the api (Deployment, 2 replicas, readiness/liveness probes on /healthz, resource requests/limits), workers (Deployment, scalable), scheduled jobs (CronJobs for scraping, grading, retraining, news ingestion), Postgres (StatefulSet + PVC), and Redis. Secrets created from env, never committed.
2. Production: migrate the VM to k3s with Traefik ingress and cert-manager TLS. Plan a cutover with minimal downtime and a rollback path.
3. CI/CD in GitHub Actions: on PR — lint, test, build images. On merge to main — push images to GHCR tagged with the git SHA, apply the prod overlay, wait for rollout, roll back automatically if it fails.
4. Observability: Prometheus + Grafana (check memory on the VM and pick the lighter setup if needed — tell me). Dashboards for API latency and RPS, cache hit rate, queue depth, job failures, active model version. Make one dashboard publicly viewable read-only.
5. Nightly pg_dump CronJob to object storage, and a documented restore test.
6. Chaos test: kill a worker pod mid-job and show the job gets retried. Record what happened in RUNBOOK.md.
```

### Done when
- [ ] Merging to main deploys automatically; a broken deploy rolls back
- [ ] Public Grafana dashboard live
- [ ] Worker-kill test passes and is documented
- [ ] Backup AND restore both tested

---

## Phase 7 — Self-hosted inference with vLLM (week 13, optional but high-signal)

### You do
- Rent a GPU instance (24GB-class is enough for a ~7–8B model) for a few hours. **Destroy it when you're done** — set a phone alarm.

### Prompt
```
Phase 7: self-hosted LLM inference. I've rented a GPU instance and can SSH in.

Deploy vLLM serving an open ~7-8B instruct model with an OpenAI-compatible endpoint. Implement a VLLMClient behind the existing LLMClient interface. Benchmark it against the Anthropic API: p50/p95 latency, tokens/sec, and throughput at 1, 8, and 32 concurrent requests, plus cost per 1,000 writeups. Explain continuous batching and show how it changes throughput as concurrency rises.

Write results to BENCHMARKS.md and a short writeup in docs/INFERENCE.md, including when self-hosting would and wouldn't make sense for this project. Remind me to shut the GPU down.
```

### Done when
- [ ] Benchmarks and INFERENCE.md written
- [ ] GPU instance destroyed

---

## Phase 8 — Ship it (week 14)

### Claude Code prompt
```
Phase 8: audit this repo like a senior engineer reviewing a new-grad candidate's project. Run /security-review. Find dead code, missing tests, docs that don't match reality, and any number in the README that isn't backed by BENCHMARKS.md. Add a Mermaid architecture diagram to the README. Give me a prioritized fix list before changing anything.
```

### You do
- Run **Cowork prompts 3 and 4** (README + blog draft, interview prep).
- Rewrite the blog post in your own voice. Recruiters can smell an unedited AI post.
- Record a 60–90 second demo video/GIF for the README.
- Update your resume, pin the repo on GitHub, put the live link on ar13.dev.
