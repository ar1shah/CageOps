# Benchmarks

Any performance claim made anywhere in this repo must have an entry here. No entry, no claim.

Each entry records the exact command, the hardware, and the date, so someone else can reproduce it.

## Entry template

```
### <what is being measured>
- Date: YYYY-MM-DD
- Hardware: CPU / RAM / OS / where it ran (laptop, VM size, ...)
- Commit: <git sha>
- Command: `<exact command>`
- Result: <numbers: p50/p95/p99, req/s, jobs/min, ...>
- Notes: warm vs cold cache, dataset size, caveats
```

## Results

### Phase 1b: ingestion worker scaling, against the LOCAL REPLAY SERVER (not the real site)
- Date: 2026-10-06 (runs finished 02:12-02:58 UTC; 26 runs, all passed validation)
- Hardware: Intel Core i9-13900KF (32 logical CPUs), 31.2 GB RAM, WSL2 (Linux 6.18.40.1-microsoft-standard-WSL2) on Windows, Python 3.12.14. Postgres 16 (`pgvector/pgvector:0.8.1-pg16`) and Redis 7.4.6 in Docker on the same machine, as are the replay server and every worker, so they share the CPUs and disk.
- Commit: `fee8135` (the code is unchanged since `ac9acca`; later commits are documentation only)
- Command: `uv run python scripts/bench_ingest.py matrix` (one run is e.g. `uv run python scripts/bench_ingest.py run --workers 4 --interval-ms 100 --latency-ms 250`). Raw records, one JSON line per run: [docs/benchmarks/ingest_2026-10-06.jsonl](benchmarks/ingest_2026-10-06.jsonl). Every table below is generated from that file.
- What it measures: a 12-month backfill (`--since 2025-09-26`) of a **synthetic** year: 52 weekly events generated from the saved Burns-card fixtures, 13 pages each, 680 requests per cold run (52 x 13 + 3 list pages + 1 empty page), served by `scripts/standin_site.py --synthetic-events 52` at **250 ms +/- 20% per response, an assumption** (seeded, so repeatable). The workers are long-running RQ `SimpleWorker` processes sharing one Redis rate limiter. Each run starts from a fresh `cageops_bench` database and Redis database 2.
- **Read every number as "against a local replay server simulating about 250 ms latency" (100 and 500 ms in series C). None is a measurement of ufcstats.com.**

**Series A: cold cache, limiter 1000 ms (how we would run it live), latency 250 ms**

| Workers | Runs | Wall-clock (median, range) | Speedup vs 1 worker | Requests | Jobs/min | Fetch p50 / p95 | Limiter wait share | Rows inserted / updated / unchanged |
|---|---|---|---|---|---|---|---|---|
| 1 | 1 | 680.2 s (680.2) | 1.00x | 680 | 60 | 266 / 477 ms | 71% | 2132 / 416 / 0 |
| 4 | 1 | 680.0 s (680.0) | 1.00x | 680 | 60 | 266 / 477 ms | 93% | 2132 / 416 / 0 |

**Series B: cold cache, limiter 100 ms, latency 250 ms**

| Workers | Runs | Wall-clock (median, range) | Speedup vs 1 worker | Requests | Jobs/min | Fetch p50 / p95 | Limiter wait share | Rows inserted / updated / unchanged |
|---|---|---|---|---|---|---|---|---|
| 1 | 3 | 190.5 s (189.1-191.0) | 1.00x | 680 | 214 | 265 / 476 ms | 0% | 2132 / 416 / 0 |
| 2 | 3 | 96.3 s (96.3-96.5) | 1.98x | 680 | 424 | 265 / 476 ms | 2% | 2132 / 416 / 0 |
| 4 | 3 | 68.5 s (68.5) | 2.78x | 680 | 596 | 266 / 477 ms | 31% | 2132 / 416 / 0 |
| 8 | 3 | 68.5 s (68.5) | 2.78x | 680 | 596 | 264 / 476 ms | 65% | 2132 / 415-416 / 0-1 |

**Series C: latency sensitivity (cold, limiter 100 ms, n=1 each; 250 ms is series B)**

| Latency | 1 worker | 4 workers | Speedup |
|---|---|---|---|
| 100 ms | 84.9 s | 68.1 s | 1.25x |
| 250 ms | 190.5 s | 68.5 s | 2.78x |
| 500 ms | 360.3 s | 94.8 s | 3.80x |

**Series D: warm cache (pages already in raw_pages, tables emptied, no network)**

| Workers | Runs | Wall-clock (median, range) | Speedup vs 1 worker | Requests | Jobs/min | Fetch p50 / p95 | Limiter wait share | Rows inserted / updated / unchanged |
|---|---|---|---|---|---|---|---|---|
| 1 | 3 | 13.2 s (13.2-13.4) | 1.00x | 0 | 3101 | n/a (no network) | 0% | 2132 / 416 / 0 |
| 4 | 3 | 3.7 s (3.7) | 3.60x | 0 | 11167 | n/a (no network) | 0% | 2132 / 407-413 / 3-9 |

**Series E: rerun on a full database (4 workers, n=1):** 3.7 s, 0 requests, rows 0 inserted / 0 updated / 2548 unchanged.

#### Why 4 workers is not 4x (Amdahl's law, with our numbers)
Amdahl's law says the part of the work that cannot be spread over more workers caps the speedup. Here that part is **the rate limiter**: every request, from any worker, must take the next slot, and slots are one interval apart. It is one toll booth in front of all the cooks.

- One worker's cycle for one request is the round trip plus its own work (parse, database): series B with 1 worker took 190.5 s for 680 requests, so **about 280 ms per request** (250 ms latency plus roughly 30 ms of work). A single worker can therefore start a request every 280 ms.
- At the **polite 1000 ms interval (series A)** the limiter only lets one request through per second, which is slower than one worker can already go (280 ms). So **1 worker already saturates the limiter** and extra workers have nothing to speed up: 4 workers took 680.0 s versus 680.2 s, a 1.00x speedup. The measured evidence: workers spent **71% (1 worker) and 93% (4 workers) of their time waiting for a limiter slot**, doing the same 680 requests in the same 680 s.
- At a relaxed **100 ms interval (series B)** a single worker is the bottleneck (it waits for a slot 0% of the time), so more workers help, up to the ceiling where the limiter becomes the bottleneck. The ceiling is about (cycle / interval) = 280 / 100 = **2.8x**, and the measurement landed on it: 1.98x with 2 workers, 2.78x with 4, and **2.78x with 8**, where 4 workers' extra capacity just queues (limiter wait share: 0% at 1 worker, 2% at 2, 31% at 4, 65% at 8). 68.5 s is the floor: 680 requests x 100 ms = 68 s, plus start-up.
- The same logic explains series C: a slower server means a longer cycle, so more workers can be kept busy before the limiter bites. At 500 ms latency the ceiling is about 530/100 = 5.3x and 4 workers got 3.80x (below the ceiling because 4 workers are fewer than the 5 or 6 it takes to saturate it); at 100 ms latency the ceiling is about 125/100 = 1.25x and 4 workers got 1.25x.
- **Warm cache (series D)** has no limiter and no network, so the only serial parts left are small: the discovery chain at the start (four list-page jobs, each enqueuing the next), start-up and Postgres. 4 workers got **3.60x** over 1 (13.2 s to 3.7 s). The run is short, so its wall-clock resolution (about 0.2 s of polling) is a visible share of 3.7 s.
- Takeaway: **workers hide latency; they cannot beat the rate limiter**, and the limiter is deliberately the thing we will not relax against the real site. In production, ingestion throughput is set by politeness (about 1 request/second), not by worker count: one worker is enough for a live backfill, and more workers only pay off for work that does not touch the network (re-parsing from `raw_pages`, which is also why the cache exists).

#### How the predictions held
| Prediction (committed before the runs) | Result | Verdict |
|---|---|---|
| A: 1 and 4 workers both about 680 s, speedup about 1.0x | 680.2 s and 680.0 s, 1.00x | held |
| B: 1 worker about 200 s | 190.5 s (189.1-191.0) | slightly faster than predicted: per-request work was about 30 ms, not 30-50 |
| B: 4 workers 70-90 s | 68.5 s | just below my range; 68.5 s is the limiter's floor, my range was too pessimistic |
| B: 8 workers about the same as 4 | 68.5 s | held |
| C: 100 ms latency, 1 worker about 100 s, 4 workers about 70 s | 84.9 s and 68.1 s | 1 worker faster than predicted (cycle about 125 ms), 4 workers held |
| C: 500 ms latency, 1 worker about 370 s, 4 workers 95-100 s | 360.3 s and 94.8 s | held (4 workers a hair under) |
| D: 4 workers 2.5-4x faster than 1 | 3.60x | held |
| E: rerun inserts 0, updates 0, requests 0 | 0 / 0 / 2548 unchanged, 0 requests | held |

#### Notes and caveats
- **Synthetic data, assumed latency.** The corpus proves the plumbing and shows the bottleneck. It says nothing about the real site's latency (the 250 ms is a guess; the sensitivity series bounds it), data volume per event, fighter overlap between cards (real cards reuse fighters, which means fewer fighter pages per fight and more cache hits), or markup quirks. The only request ever made to the real site in this phase (the probe, 161 ms) returned the bot-challenge page, so it says nothing about content-page latency either.
- **One machine.** Workers, replay server, Postgres and Redis share the CPUs and the WSL2 disk. Series A, C and E are n=1; series B and D are n=3 and the repeats agree within about 1-2%.
- **Rows: inserted versus updated.** Per cold run: 2132 inserted (52 events + 208 fights + 416 fighter rows + 416 fight-totals rows + 1040 per-round stat rows), 416 updated (every fighter's bio, written by the fighter job after the fight job created the name-only row), 0 unchanged. At 8 workers (cold) and at 4 workers (warm) the fighter split varies by a few rows (for example 407 updated and 9 unchanged in a warm run) because of an **ordering race**: a fighter job can run before the fight job that creates that fighter's name-only row; then the fighter job does the insert and the fight job's later "create if missing" counts as unchanged. The totals (2548 rows) and the final database are identical in every run, so it is a counting detail, not a data problem; I haven't proven the cause beyond the pattern (it appears only when jobs run concurrently and fast).
- **Burst workers would have invalidated this.** `worker --burst` exits when the queue is empty and the queue starts with one discovery job, so 3 of 4 workers would quit immediately; these runs use long-running workers.
- **Cache keys include the URL**, so the replay server uses one fixed port (18099) for cold and warm runs; with a different port the warm run would miss its own cache.
- Validation per run: 0 dead letters, 52 events / 208 fights / 416 fighters in the database, exactly 680 requests (cold) or 0 (warm, rerun), nothing written on a rerun. A run that failed any of these would have been recorded with `valid: false` and excluded; none did.

### Phase 1b ingestion benchmark: predictions, written before the runs (2026-10-06)

Everything below this heading comes from the **local replay server** (a generated year of events,
`scripts/standin_site.py --synthetic-events 52`), never from ufcstats.com, which is still behind a bot
challenge. The page latency (250 ms) is an **assumption**, not a measurement of the real site. These
predictions were committed before any run, so the results can contradict them.

- A (limiter 1000 ms, latency 250 ms, cold): 1 worker about 680 s, 4 workers about 680 s. The limiter lets
  one request through per second, so ~680 requests cannot finish in less than ~680 s whatever the worker
  count. Predicted speedup: about 1.0x.
- B (limiter 100 ms, latency 250 ms, cold): 1 worker about 200 s (~3.5 requests/s: a 250 ms round trip plus
  ~30-50 ms of parsing and database work). 4 workers about 70-90 s (the limiter ceiling is 10 requests/s, so
  680 requests need at least 68 s). 8 workers about the same as 4.
- C (latency sensitivity, limiter 100 ms): at 100 ms latency 1 worker about 100 s and 4 workers about 70 s;
  at 500 ms latency 1 worker about 370 s and 4 workers about 95-100 s.
- D (warm cache, no network): 4 workers 2.5-4x faster than 1, limited by Postgres and the machine, not a limiter.
- E (rerun on a full database): 0 rows inserted, 0 updated, only unchanged; 0 requests.

### Phase 2b-1: fighter name search (D-040) and the stats-gap counts (D-039)
- Date: 2026-10-15
- Hardware: Intel Core i9-13900KF (32 logical CPUs), 31 GiB RAM, WSL2 (Linux 6.18.54.1-microsoft-standard-WSL2); Python 3.12.14; Postgres 16.12 (`pgvector/pgvector:0.8.1-pg16`) in Docker on the same machine; warm cache, no other load.
- Commit: `d72ee77` plus the commit that adds this entry. Database: 2,760 fighters, features build `load_runs.id` 38.
- Command (the scan): `docker compose exec -T postgres psql -U cageops -d cageops -At -c "EXPLAIN (ANALYZE) SELECT id, name FROM fighters WHERE name ILIKE '%silva%'"`, ten times.
- Command (the endpoint's query): `cageops_api.queries.search_fighters(conn, q, limit=20, offset=0)` called 200 times per query on one connection after 20 warm-up calls, timed with `time.perf_counter` in a short script (the query only: no HTTP, no JSON, no pool wait).
- Result:

| what | value |
|---|---|
| bare `ILIKE '%silva%'` as a sequential scan, 27 matches of 2,760 rows | Execution Time 0.74 to 0.86 ms over 10 runs (planning 0.27 ms) |
| `search_fighters("silva")`, 27 matches | median 3.15 ms, p95 3.82 ms |
| `search_fighters("alex")`, 39 matches | median 3.33 ms, p95 4.06 ms |
| `search_fighters("jon jones")`, 1 match | median 2.25 ms, p95 2.57 ms |

- Notes: D-040's first draft said 0.55 ms from one earlier `EXPLAIN ANALYZE`; ten runs on the same data gave 0.74 to 0.86 ms, so the entry says that. The endpoint's query costs more than the scan because each of the (up to 20) returned rows also looks up its current division, fight count and last fight date. These are database times for one process on one machine, not request latency: the HTTP numbers come from the 2b-2 load test.

### Phase 2b-1: the stats-gap counts D-039 quotes
- Date: 2026-10-15
- Hardware: not a timing; counts do not depend on it.
- Commit: `d72ee77` plus the commit that adds this entry. History through 2026-10-03; features build `load_runs.id` 38.
- Command: `uv run python -m cageops_worker.features stats-gap --since 2026-05-17` (read-only)
- Result:

| what | value |
|---|---|
| completed fights | 8,779 |
| completed fights without per-round stats (both sides needed) | 245: 224 with a Wikipedia result, 21 with a ufcstats result |
| fighters with a completed fight | 2,760 |
| fighters whose rates miss at least one fight | 442: 406 only Wikipedia-sourced, 36 only ufcstats-era (1990s), 0 both |
| fighters with a completed fight on or after 2026-05-17 | 406 (the ROADMAP query, which counts them whether or not the fight has stats) |
| of those, with a fight lacking stats | 406 |

- Notes: the 406 matches the number the ROADMAP carry-over quotes for 2026-10-14. The test-slice log losses `data_quality.known_effect` quotes (0.6878 over 223 fights, 0.6566 over 330) are the Phase 2a entry above and are not re-measured here.

### Phase 2b-2: API load test, RESULTS (2026-10-15)
- Date: 2026-10-15
- Hardware: Intel Core i9-13900KF (32 logical CPUs), 31 GiB RAM, WSL2 (Linux 6.18.54.1-microsoft-standard-WSL2); Docker 29.8.0. **k6, the API, Postgres and Redis share this one machine and compete for its CPUs, and k6 reaches the API over the compose bridge network: these are not the numbers a remote client would see.**
- Commit: `132fd4c` plus the commit that adds this entry. API image `cageops-api:local` (612 MiB), one uvicorn process (a 40-thread pool for sync routes), `OMP_NUM_THREADS=1`. k6 2.3.0. Postgres 16.12 and Redis 7.4.6 in compose.
- Data: database `cageops_loadtest` (a copy of the dev database, 8,779 completed fights, plus a **synthetic** 36-bout upcoming list on three labelled cards, with a features rebuild), Redis database 3, 60,000 unique same-division pairs from the real data, a 500-pair warm set. Model `20261014-225801`.
- Commands: `uv run python scripts/loadtest/prepare_db.py --force`, `uv run python scripts/loadtest/make_inputs.py`, then `uv run python scripts/loadtest/run_loadtest.py --fresh --duration 30s` (k6 itself: `docker compose --profile api --profile loadtest run --rm k6 run /scripts/api.js` with `SCENARIO`, `VUS`, `DURATION`, `RATE`). The rate limiter is **off** for every run except those marked "limiter ON" (all load comes from one client, and a limit it would hit measures the limiter and not the API). Cache, pool and timeout are the shipped defaults unless a row says otherwise (pool 5 + 5, pool wait timeout 30 s for these runs; the default was changed to 5 s afterwards, see below).
- Method: each run restarts the API for its configuration, empties the cache database, runs k6 for 30 s, and scrapes `/metrics` before and after. Warm and mixed runs first make one unmeasured pass over the 500 warm pairs. Every run is validated; the table says which were not. n = 1 per cell, one machine, no confidence intervals: the repeat runs below (`warm` off twice) differ by 0.5%, which is the only noise estimate there is.

**Throughput and latency, `POST /predict` as seen by k6** (closed loop: the clients wait for each answer; open model: requests arrive at a fixed rate)

| run | load | req/s | p50 ms | p95 ms | p99 ms | failed | valid |
|---|---|---|---|---|---|---|---|
| cold cache (every pair new) | 16 VUs | 219 | 71.1 | 99.2 | 116 | 0 | yes: 0 hits, 6,582 misses |
| warm cache, limiter off | 16 VUs | 1,364 | 11.1 | 15.2 | 19.6 | 0 | yes: 40,918 hits, 0 misses |
| warm cache, limiter ON | 16 VUs | 893 | 17.5 | 22.2 | 25.6 | 0 | yes: 0 refused |
| warm cache, limiter off (again) | 16 VUs | 1,358 | 11.2 | 15.1 | 20.4 | 0 | yes |
| mixed (predict 60%, search 20%, profile 10%, upcoming 10%) | 16 VUs | 382 | predict 23.4 | 72.2 | 99.6 | 0 | yes |
| cold, open model at 50% of cold peak | 110 req/s | 110 | 4.7 | 6.8 | 22.0 | 0 | yes |
| warm, open model at 50% of warm peak, limiter off | 682 req/s | 682 | 0.85 | 2.21 | 18.6 | 0 | yes |
| warm, same arrival rate, limiter ON | 682 req/s | 675 | 1.28 | **218.7** | 233 | 0 | **no: 216 iterations dropped, the API could not keep the rate** |

Mixed, by endpoint (k6, ms): predict p50 23.4 / p95 72.2; `GET /fighters?search=` p50 50.6 / p95 83.0; `GET /fighters/{id}` p50 53.8 / p95 87.1; `GET /events/upcoming` (36 bouts, cached after the first) p50 40.9 / p95 69.3. At 16 clients on one saturated process every endpoint queues, which is why a cheap cached `upcoming` sits at 41 ms.

**The limiter's per-request cost, with the numbers side by side**

| | limiter off | limiter ON | difference |
|---|---|---|---|
| closed loop, 16 VUs: req/s | 1,364 (1,358 again) | 893 | **-34.5%** |
| service time per request (1 / req/s) | 0.733 ms | 1.120 ms | **+0.39 ms (+53%)** |
| p50 / p95 / p99 (ms) | 11.1 / 15.2 / 19.6 | 17.5 / 22.2 / 25.6 | +6.4 / +7.1 / +6.0 |
| open model, 682 req/s: p50 / p95 / p99 (ms) | 0.85 / 2.21 / 18.6 | 1.28 / **218.7** / 233 | the limiter-on server could not keep 682 req/s without dropping 216 iterations |
| the same open-model run in the first pass of this test (image without `OMP_NUM_THREADS=1`) | 0.84 / 2.12 / 18.1 | 1.19 / **68.7** / 148 | valid there; the tail is large in both passes |

Reading it: 0.39 ms is one Redis round trip (an `EVALSHA`) plus a thread hop (`anyio.to_thread`), and because a cached prediction costs only 0.73 ms to serve, it is a large fraction. The open-model rows are the more important finding: at an arrival rate that is 50% of the limiter-off capacity, the limiter-on server is at about 75% utilisation (682 x 1.12 ms) and its tail latency explodes (p95 69 to 219 ms) while its throughput does not look much worse. A cheap request plus a per-request thread hop plus Redis is GIL and scheduling contention, not Redis latency (the cache stage averaged 2.2 ms with the limiter on against 3.1 ms off in the closed loop, so Redis itself is not slower). I did not isolate the cause further.

**Where a prediction spends its time** (`api_predict_stage_seconds`, mean per request; these are the server's own timers)

| stage | cold, uncontended (110 req/s) | cold, 16 VUs saturated | warm, uncontended (682 req/s) | warm, 16 VUs saturated |
|---|---|---|---|---|
| `cache` (Redis GET, and SET on a miss) | 0.24 ms | 2.90 ms | 0.29 ms | 3.12 ms |
| `db` (pooled connection wait + 4 queries) | **2.80 ms** | 41.5 ms (pool wait p50 7.7 ms, p95 43.7 ms) | none | none |
| `assemble` (row to model input) | 0.05 ms | 0.03 ms | none | none |
| `model` (TreeSHAP both orders, win, method) | **0.76 ms** | 8.4 ms | none | none |
| `build` + `serialize` | 0.13 ms | 0.13 ms | 0.05 ms | 0.05 ms |
| request total, server p50 | 4.1 ms | 75.6 ms | 0.58 ms | 9.2 ms |

**The cold/warm gap, explained from those timers.** Throughput is 219 against 1,364 req/s, 6.2 times; uncontended latency is 4.7 against 0.85 ms, 5.6 times. Of the 3.9 ms a miss costs beyond a hit, the database stage is 2.8 ms (four queries, each a round trip plus SQLAlchemy and psycopg Python work, and two of them scan `completed_fights`) and the model 0.76 ms; Redis, assembly and serialization are about a tenth. A hit skips both. Under saturation the same stages inflate (db 2.8 to 41.5 ms) because 16 threads share one process: that inflation is queueing for the interpreter and the pool, not the database doing more work (Postgres averaged 37% of one core at 200 req/s while the API process used 130%).

**Is the process the limit? Yes.** `docker stats` during a cold run at 16 VUs: API 221% of a core, Postgres 37%, k6 7%, Redis 2% (default OpenMP threads); API 132%, Postgres 36%, k6 7%, Redis 0.4% with `OMP_NUM_THREADS=1`. Cold throughput (219 req/s) is about 1 / the uncontended latency (1 / 4.74 ms = 211 req/s): the one Python process does the work essentially serially. More connections, more threads and a bigger pool do not help; more processes would.

**`OMP_NUM_THREADS=1` (a finding, now in the Dockerfile).** LightGBM predicts with OpenMP and by default starts, and spin-waits on, one thread per CPU per call; for one-row predictions from a thread pool that is pure overhead. Same machine, same data, the image without and with the variable:

| | default OpenMP | `OMP_NUM_THREADS=1` |
|---|---|---|
| `model` stage, uncontended (110 req/s) | 3.24 ms | **0.76 ms** |
| uncontended cold request, p50 (k6) | 7.26 ms (at 101 req/s) | **4.74 ms** (at 110 req/s) |
| cold, 16 VUs: req/s / p50 | 203 / 76.8 ms | **219 / 71.1 ms** (+8%) |
| API CPU during a cold run | 221% | 132% |
| warm, mixed | 1,370 / 378 req/s | 1,364 / 382 req/s (no model on the warm path: unchanged) |

**The database pool** (cold cache, 64 clients: far more than 10 connections, so the pool is the queue)

| pool (size + overflow), wait timeout | req/s | p50 / p95 ms | peak checked out | pool wait p50 / p95 / p99 ms | checkout timeouts |
|---|---|---|---|---|---|
| 5 + 5 (the default), 30 s | 212.5 | 293 / 436 | 10 | 230 / 473 / 495 | 0 |
| 10 + 10, 30 s | 217.7 | 288 / 359 | 20 | 31 / 98 / 407 | 0 |
| 20 + 20, 30 s | 210.3 | 300 / 368 | 40 | 8.3 / 44.7 / 49.5 | 0 |
| 2 + 0, 1 s | 228.7 | 273 / 357 | 2 | 290 / 479 / 496 | 0 (never exhausted: the wait never reached 1 s) |
| 2 + 0, **50 ms** | 515 (including failures) | 118 / 176 | 2 | 71 / 98 / 126 | **11,621 of 15,507 requests (74.9%)**, each a `503 database_busy` |
| at 16 clients (the cold run above), 5 + 5, 30 s | 219 | 71 / 99 | 10 | 7.7 / 43.7 / 49.0 | 0 |
| at 50% of cold peak (110 req/s), 5 + 5, 30 s | 110 | 4.7 / 6.8 | 7 | 0.3 / 0.49 / 1.14 | 0 |

What it shows: **throughput does not move with pool size** (212.5, 217.7, 210.3 req/s for 5, 10 and 20 connections each way, within 3.4% of one another, with p50 about 290 to 300 ms = 64 clients / 215 req/s in every case). A bigger pool moves the waiting from the pool queue to the interpreter queue (pool wait p95 473 to 45 ms, while the model stage rose from 9.9 to 14.0 ms and the cache stage from 3.4 to 32.7 ms). A pool of 2 still served about 129 successful req/s. The 50 ms diagnostic shows the safety valve works: three in four requests were refused quickly with a 503 rather than queued, and every failure was a counted pool timeout (failed requests = timeouts exactly).

**Pool recommendation, by the rule written before the runs.** The rule was: smallest size where pool-wait p95 is under 5 ms or throughput is within 5% of the next size up, **and** peak checked-out at or under 80% of the ceiling. Under 64 clients the second clause cannot hold for any size (a closed loop with more clients than connections always reaches the ceiling), so I applied the rule at the load the process can sustain, 50% of cold peak: 5 + 5 peaks at 7 of 10 (70%), pool wait p95 0.49 ms, no timeouts, and throughput at overload is within 2.4% of 10 + 10. **Keep `DB_POOL_SIZE=5` and `DB_MAX_OVERFLOW=5`.** **Change `DB_POOL_TIMEOUT_S` from 30 to 5**: the longest wait in a run that was not deliberately starved was 0.5 s, a request that has waited 5 s is not going to be answered usefully, and 30 s keeps a client, a thread and a request open far past anything that will still be listening; a fast 503 with `Retry-After` is what the limiter and the frontend can act on. This is the one default changed (`config.py`, `.env.example`, compose). The 5 s value is a judgment bounded by these measurements (the starved run proves the 503 path, not that 5 s is optimal), not a tuned optimum.

**Predictions against what happened** (the predictions are the entry below; the cold and pool rows were informed by a dry run, as that entry says)

| | predicted | measured | verdict |
|---|---|---|---|
| cold req/s; p50; p95 | about 200 (150 to 260); 75; 110 ms | 219; 71; 99 ms | inside |
| warm, limiter off: req/s; p50; p95 | about 800 (500 to 1,200); 18; 30 ms | 1,364; 11.1; 15.2 ms | **outside, faster**: a hit is about 0.73 ms of process time, not 1.2 |
| limiter's service-time cost | +0.3 to +0.7 ms | +0.39 ms | inside |
| limiter's effect on req/s; p50; p95 | -15% (5 to 30%); +3; +5 ms | -34.5%; +6.4; +7.1 ms | **outside, worse** (the baseline was cheaper than I assumed, so the same 0.39 ms is a bigger share) |
| warm off, repeat | within 10% | within 0.5% | inside |
| mixed req/s | about 430 (300 to 600) | 382 | inside |
| rate_warm p50; p95; p99 | 3; 8; under 20 ms | 0.85; 2.21; 18.6 ms | better than predicted |
| rate_cold p50; p95 | 20; 45 ms | 4.7; 6.8 ms | **outside, far faster**: I assumed 12 to 18 ms of server time, it is 4 |
| pool 5 + 5: req/s; p50; wait p95; peak; timeouts | about cold_16; about 300 ms; hundreds of ms; 10; 0 | 212; 293 ms; 473 ms; 10; 0 | inside |
| pool 10 + 10 over 5 + 5 | +10 to +25% req/s | +2.4% | **outside**: no gain |
| pool 20 + 20 vs 10 + 10 | within 10% | -3.4% | inside (and peak "well under 40": it was 40, all of it, because 64 clients) |
| cold/warm throughput ratio | 4 to 5 times | 6.2 times | **outside, higher** |

**What I would do about it, in order of payoff** (none is in this PR): (1) make the limiter's Redis call without the thread hop (an `redis.asyncio` client), because 0.39 ms and a 69 to 219 ms p95 at three-quarters utilisation is a poor price for a cheap endpoint; (2) cut the per-request queries from four to two (names with rows, divisions with counts), since the database stage is 2.8 of 3.9 uncontended milliseconds and most of that is Python-side round-trip cost; (3) more processes or replicas, since one process is the limit (Phase 6; `uvicorn --workers` needs Prometheus multiprocess mode, which this API deliberately does not use).

- Caveats: n = 1 per cell; one machine on which the load generator competes with the system under test; WSL2 and Docker Desktop networking; a synthetic upcoming card; 30 s windows; k6 `http_req_duration` includes the compose network hop. The first pass of the whole matrix (default OpenMP, kept in `data/bench/api/results_matrix1_omp_default.jsonl`) agrees with the second everywhere except where the table says OpenMP mattered. Raw per-run records are in `data/bench/api/results.jsonl` (gitignored).

### Phase 2b-2: API load test, PREDICTIONS (written before the runs, 2026-10-15)
- Written: 2026-10-15, before the first measured run, and committed before it (D-026's practice).
- **Disclosure:** the harness was exercised once before this was written, with an 8 s cold run at 16 VUs, to find bugs in it. That run was valid and showed about 204 req/s, predict p50 about 76 ms, p95 about 105 ms, a connection pool at its ceiling (peak 10 of 5+5) and a database stage averaging about 42 ms. **The cold and pool predictions below are therefore informed by it** and are not blind. Every other prediction was made without a measurement of that scenario. The dry run's record is kept in `data/bench/api/dryrun/` and is not part of the results.
- Setup being predicted: the API image from this repo (one uvicorn process, 40-thread pool, pool 5+5 unless stated) in compose, Postgres 16 and Redis 7.4 in compose, k6 2.3.0 on the compose network, one machine (i9-13900KF, WSL2), 30 s runs, `cageops_loadtest` database (the dev data plus a synthetic 3-card, 36-bout upcoming list), 60,000 unique same-division pairs, a 500-pair warm set.

| run | prediction | why |
|---|---|---|
| `cold_16` (16 VUs, every pair new) | about 200 req/s (range 150 to 260); predict p50 about 75 ms, p95 about 110 ms; the database stage is more than half of server time (it includes the wait for a pooled connection), the model about 20%, Redis about 5 to 10% | the dry run; four queries, two of which scan `completed_fights` per request, plus TreeSHAP in both orders |
| `warm_16_off_1` (16 VUs, 500 pairs, all hits, limiter off) | about 800 req/s (range 500 to 1,200); p50 about 18 ms, p95 about 30 ms | a hit is routing, a Redis GET, a flip and JSON, about 1.2 ms of one Python process's time; at 16 VUs p50 is roughly 16 over the throughput |
| `warm_16_on` (limiter on, a limit nobody reaches) | **a service time about 0.3 to 0.7 ms per request higher than limiter off**, so about 15% fewer req/s (range 5 to 30%); p50 about 3 ms higher at 16 VUs, p95 about 5 ms higher | one more Redis round trip (an `EVALSHA`) and one thread hop per request |
| `warm_16_off_2` (limiter off again) | within 10% of `warm_16_off_1` | to see how much the machine drifts between adjacent runs |
| `mixed_16` | about 430 req/s (range 300 to 600); predict p50 slower than warm, search and profile p50 about 15 to 30 ms | an average service time of about 2.3 ms: 48% cached predicts at 1.2 ms, 12% new pairs at about 5 ms, searches and profiles at 3 to 4 ms |
| `rate_warm` (50% of warm peak, open model) | predict p50 about 3 ms, p95 about 8 ms, p99 under 20 ms; no dropped iterations | no queueing at half load |
| `rate_cold` (50% of cold peak) | predict p50 about 20 ms, p95 about 45 ms | an uncontended cold request is about 12 to 18 ms of server time |
| `pool_64_5_5` (cold, 64 VUs) | throughput about the same as `cold_16` (the pool is the limit), predict p50 about 300 ms, pool wait p95 in the hundreds of ms, peak checked out 10, **0 timeouts** (the 30 s timeout is never reached) | 64 clients, 10 connections |
| `pool_64_10_10` | 10 to 25% more req/s than 5+5, pool wait p95 down by more than half, peak 20 | more connections, until something else saturates |
| `pool_64_20_20` | within 10% of 10+10 (the single process or Postgres is the limit by then); peak well under 40 | the gain saturates |

- **The cold/warm gap**, predicted: a throughput ratio of about 4 to 5 times, with the difference being the database stage (four queries and a pooled connection) and the model (TreeSHAP in both orderings), which a hit skips entirely; a hit still pays routing, Redis and JSON.
- **The pool recommendation rule**, fixed now so it cannot bend to the results: choose the smallest `DB_POOL_SIZE` + `DB_MAX_OVERFLOW` for which, at the heaviest tested load, pool-wait p95 is under 5 ms or throughput is within 5% of the next size up, and the peak checked-out is at or under 80% of the ceiling. Keep the default if it already meets that at the load the API is expected to carry.

### Phase 2b-2: the API image's size (D-045)
- Date: 2026-10-15
- Hardware: Intel Core i9-13900KF (32 logical CPUs), 31 GiB RAM, WSL2 (Linux 6.18.54.1-microsoft-standard-WSL2); Docker 29.8.0 (Docker Desktop, containerd image store); `linux/amd64`.
- Commit: `d739704`
- Command: `docker build -f infra/docker/api.Dockerfile -t cageops-api:local .`, then `docker image inspect cageops-api:local --format '{{.Size}}'`, `docker history cageops-api:local`, and `docker run --rm --entrypoint sh cageops-api:local -c 'du -sxm / ; du -sm /app/.venv/lib/python3.12/site-packages/* | sort -rn | head'`. The size check CI runs is `scripts/check_api_image.sh cageops-api:local` (budget 800 MiB).
- Result:

| what | value |
|---|---|
| `docker image inspect .Size` | **641,896,418 bytes (612 MiB)** |
| sum of the layer sizes in `docker history` | about 494 MB |
| files inside the container (`du -sxm /`) | 459 MiB |
| the python:3.12.15-slim-bookworm base alone (`.Size`) | 197,098,586 bytes |
| `/app/.venv` (the installed dependencies) | 328 MiB (344 MB layer) |
| largest installed packages | scipy 109 MiB (+30 MiB `scipy.libs`), numpy 43 MiB (+27 MiB `numpy.libs`), sqlalchemy 28, uvloop 16, psycopg binary libs 11, lightgbm 10 |
| `libgomp1` | 811 kB |
| layers | 7 |
| cold build of the app layers, base images already pulled | 17.6 s wall clock (7.8 s of it downloading the locked wheels) |

- Notes: three numbers, one image: `.Size` is what Docker Desktop reports and what the budget uses; the layer sum and the filesystem total are smaller because the containerd store's `.Size` counts the unpacked snapshot differently. The big item is scipy (about 140 MiB with its libraries), which LightGBM depends on, so it cannot be dropped without dropping the model library. Nothing here was minimized: no `apt` cache (removed in the same layer), no compiler, no uv, no tests. One build on one machine, n=1.

### Phase 2b-2: the input fingerprint the cache key polls (D-042)
- Date: 2026-10-15
- Hardware: Intel Core i9-13900KF (32 logical CPUs), 31 GiB RAM, WSL2 (Linux 6.18.54.1-microsoft-standard-WSL2); Python 3.12.14; Postgres 16.12 in Docker on the same machine; warm cache, no other load.
- Commit: the commit that adds this entry. Database: 8,779 completed fights, 17,558 `fight_totals` rows, features build `load_runs.id` 38.
- Command: `cageops_common.db.fingerprint.features_input_fingerprint(conn)` called 15 times on one connection after one warm-up call, timed with `time.perf_counter` in a short script; and, for comparison, `SELECT count(*), max(id) FROM completed_fights` plus `SELECT max(id) FROM load_runs WHERE source = 'features'`, 30 times.
- Result:

| what | time |
|---|---|
| `features_input_fingerprint` (a hash of every completed fight, its date, both bios and its stats) | median 50 ms, range 47 to 63 ms |
| the cheap watermark (a count, a max id, the latest build id) | median 1.1 ms |

- Notes: the API calls the fingerprint in a background thread every `MODEL_POLL_INTERVAL_S` (30 s), not per request, so the 50 ms is about 0.17% of one database connection's time, and no request waits on it. It was chosen over the watermark because it also notices a result that changed or a bio that was filled in, which a count cannot. It equalled the latest build's stored `inputs_sha256` on this database. This is database time for one process on one machine.

### Phase 2a: model quality against the baselines (real data, model version `20261014-225801`)
- Date: 2026-10-14
- Hardware: the same machine as below (Intel Core i9-13900KF, WSL2). Not a performance number; quality does not depend on it.
- Commit: `41faa11`
- Command: `uv run python -m cageops_worker.features rebuild` then `uv run python -m cageops_trainer train` (seed 42, defaults). The full report is stored in `model_versions.metrics` and printed by `train`.
- Data: features build `load_runs.id` 36, `output_sha256` `5b23d187c71067b4ac7427ebfbc98c469b9d02dcc47dfddf857e36258a29e468`. The model was fit on the training split only.

**Splits (D-031).** Time-based, never random. Boundaries are dates.

| split | event dates | fights |
|---|---|---|
| train | 1994-03-11 to 2024-09-28 | 7,706 |
| validation | 2024-10-05 to 2025-09-27 | 512 |
| test | 2025-10-04 to 2026-10-03 | 553 |

**Win/loss, test split (553 fights).** P(fighter A wins), fighters averaged over both orderings.

| | fights | accuracy | log loss | Brier |
|---|---|---|---|---|
| model | 553 | 0.591 | 0.6692 | 0.2383 |
| experience/ranking heuristic | 553 | 0.416 | 0.7077 | 0.2573 |
| coin flip (0.5) | 553 | n/a | 0.6931 | 0.2500 |
| **model, only the 241 fights with odds** | 241 | 0.640 | 0.6392 | 0.2241 |
| **betting favorite, same 241 fights** | 241 | 0.744 | 0.5536 | 0.1863 |
| heuristic, same 241 fights | 241 | 0.395 | 0.7109 | 0.2589 |

- **Model vs the favorite, on the same 241 fights: the model loses.** Log loss 0.6392 against 0.5536; difference (model minus favorite) +0.0856, 95% paired-bootstrap interval [+0.0473, +0.1222] (1,000 resamples, seed 42). The interval excludes 0. On validation (476 fights with odds) it is the same shape: model 0.6443, favorite 0.5824, difference +0.0618 [+0.0289, +0.0917]. This is the expected result: the odds are likely closing lines (`captured_at` is NULL, so they are not proven pre-fight), and a market price can reflect things the features never see (injuries, camp news, line movement).
- **Odds coverage: 241 of 553 test fights.** mdabbert odds end 2026-03-28, so none of the 223 fights from 2026-05-17 have odds. The favorite is scored only where odds exist; exactly equal prices (3) are left out of its accuracy.
- **Against a coin flip** the model gains 0.0240 log loss on the whole test split. Accuracy excludes 20 fights where the model says exactly 0.5 (two fighters with nothing to tell them apart).
- **The heuristic's edge reversed.** Its pick won 53.8% of 6,598 training fights, 50.0% of 470 validation fights and 41.6% of 503 test fights. Its probabilities were fit on train, so on test it scores worse than a coin flip (0.7077 against 0.6931). The rule's main input, `prior_ufc_fights`, is also a model input.

**Test slices.**

| | fights | accuracy | log loss | Brier |
|---|---|---|---|---|
| before 2026-05-17, model | 330 | 0.598 | 0.6566 | 0.2325 |
| before 2026-05-17, heuristic | 330 | 0.434 | 0.7052 | 0.2560 |
| from 2026-05-17 (Wikipedia, no stats), model | 223 | 0.580 | 0.6878 | 0.2469 |
| from 2026-05-17 (Wikipedia, no stats), heuristic | 223 | 0.387 | 0.7114 | 0.2591 |

The fights from 2026-05-17 have no per-round stats, so their fighters' rate features miss their most recent bouts. The model is a little worse there (log loss 0.6878 against 0.6566), but with 223 fights that gap is within noise.

**Validation split (512 fights).**

| | fights | accuracy | log loss | Brier |
|---|---|---|---|---|
| model | 512 | 0.633 | 0.6492 | 0.2287 |
| experience/ranking heuristic | 512 | 0.500 | 0.6959 | 0.2514 |
| model, the 476 fights with odds | 476 | 0.643 | 0.6443 | 0.2263 |
| betting favorite, same fights | 476 | 0.686 | 0.5824 | 0.1988 |

**Calibration, test split** (10 equal-count bins of predicted P(A); expected calibration error 0.0647). Each bin holds about 55 fights, so an observed rate moves about 7 points on noise alone.

| bin | fights | predicted range | mean predicted | observed rate |
|---|---|---|---|---|
| 1 | 56 | 0.23 to 0.34 | 0.302 | 0.196 |
| 2 | 56 | 0.34 to 0.40 | 0.372 | 0.429 |
| 3 | 56 | 0.40 to 0.45 | 0.429 | 0.393 |
| 4 | 55 | 0.45 to 0.49 | 0.471 | 0.491 |
| 5 | 55 | 0.49 to 0.52 | 0.503 | 0.636 |
| 6 | 55 | 0.52 to 0.56 | 0.541 | 0.545 |
| 7 | 55 | 0.56 to 0.61 | 0.587 | 0.491 |
| 8 | 55 | 0.61 to 0.64 | 0.622 | 0.636 |
| 9 | 55 | 0.64 to 0.68 | 0.659 | 0.582 |
| 10 | 55 | 0.68 to 0.79 | 0.721 | 0.618 |

Reading it: there is no consistent direction. The lowest bin is more extreme than predicted (mean 0.302, observed 0.196) and the highest is less extreme (0.721, observed 0.618). One standard error of an observed rate over 55 fights is about 6.7 points (sqrt(0.25 / 55)), and no bin is more than about 2 standard errors from its prediction. So calibration is not clearly off and not demonstrated either; 553 fights cannot tell. A calibration step (isotonic or Platt) is the obvious next experiment and is not done here.

**Method of victory (ko_tko / submission / decision; DQ excluded).** Fight-level, symmetric in the fighters. Baseline: the training class frequencies (decision 0.465, ko_tko 0.334, submission 0.201).

| | fights | accuracy | log loss | Brier (multiclass) |
|---|---|---|---|---|
| validation, model | 512 | 0.541 | 0.9541 | 0.5737 |
| validation, class prior | 512 | 0.525 | 1.0030 | 0.6046 |
| test, model | 552 | 0.491 | 1.0187 | 0.6161 |
| test, class prior | 552 | 0.424 | 1.0520 | 0.6392 |

**Schedule-columns ablation (D-032).** The method model trained once more with `is_title_fight` and `scheduled_rounds` added, scored on validation only (512 fights, every one filled): log loss 0.9481 with them against 0.9541 without, a difference of -0.0060. That is what leaving them out costs.

**What this does and does not show.** One split, 553 test fights, 241 with odds. Differences of a few hundredths of log loss between the model and the heuristic or the coin flip are inside what a different year could change. The single clear result is that the model does not beat the betting favorite.

### Phase 2a: one training run, wall clock
- Date: 2026-10-14
- Hardware: Intel Core i9-13900KF (32 logical CPUs), 31 GiB RAM, WSL2 (Linux 6.18.54.1-microsoft-standard-WSL2) on Windows, Python 3.12.14, LightGBM 4.7.0, numpy 2.5.4. Postgres 16 (`pgvector/pgvector:0.8.1-pg16`) in Docker on the same machine.
- Commit: `41faa11`
- Command: `uv run python -m cageops_trainer train`
- Result: 4.83 s, 4.75 s and 4.76 s wall clock for three consecutive runs (an earlier run took 4.79 s).
- Notes: includes `uv` and Python start-up, reading 17,558 feature rows and 8,771 fights in one snapshot, fitting three models (win/loss, method, and the ablation model), scoring, the 1,000-resample bootstrap, writing the artifacts and the registry row. LightGBM runs with `num_threads=1` and `deterministic=True` on purpose (D-033), so this is a single-threaded number; it says nothing about other hardware or a larger table. n=3, warm Postgres, no other load.

### Phase 2a: the data counts D-031 and D-032 quote
- Date: 2026-10-14
- Hardware: not a timing; counts do not depend on it (the same machine and Postgres as above).
- Commit: the commit that adds this entry (the database was at features build `load_runs.id` 36, history through 2026-10-03).
- Command: the query below, run read-only against the dev database: `docker compose exec -T -e PGOPTIONS='-c default_transaction_read_only=on' postgres psql -U cageops -d cageops < query.sql`
- Result:

| what | value |
|---|---|
| completed fights | 8779 |
| with a decided winner (`outcome = 'win'`) | 8771 |
| draws / no contests / unknown | 3 / 3 / 2 |
| DQ wins | 24 |
| lower-id fighter (A) win share | 0.5014 |
| red corner win share (fights with a corner) | 0.6419 (8548 fights) |
| last event with mdabbert odds | 2026-03-28 |
| winner fights from 2026-05-17 (Wikipedia period) | 223 |
| train / val / test winner fights (rolling 365 days) | 7706 / 512 / 553 |
| test fights with mdabbert odds | 241 |
| fighters with a completed fight from 2026-05-17 (their rate features exclude it; second query below) | 406 |

- Notes: these are the numbers the decisions are built on. They move when ingestion adds fights, so a later reader should re-run the query rather than trust the table.

```sql
WITH lf AS (
  SELECT f.id, e.event_date, f.outcome, f.method, f.winner_id, f.fighter_a_id, f.red_fighter_id,
         f.status, f.result_source,
         EXISTS (SELECT 1 FROM odds o WHERE o.fight_id = f.id AND o.source = 'mdabbert') AS has_odds
  FROM fights f JOIN events e ON e.id = f.event_id WHERE f.status = 'completed')
SELECT 'completed fights' AS what, count(*)::text AS value FROM lf
UNION ALL SELECT 'with a decided winner (outcome=win)', count(*)::text FROM lf WHERE outcome = 'win'
UNION ALL SELECT 'draws / no contests / unknown', concat_ws(' / ', count(*) FILTER (WHERE outcome='draw'), count(*) FILTER (WHERE outcome='no_contest'), count(*) FILTER (WHERE outcome='unknown')) FROM lf
UNION ALL SELECT 'DQ wins', count(*)::text FROM lf WHERE outcome = 'win' AND method = 'dq'
UNION ALL SELECT 'lower-id fighter (A) win share', round(avg((winner_id = fighter_a_id)::int)::numeric, 4)::text FROM lf WHERE outcome = 'win'
UNION ALL SELECT 'red corner win share (fights with a corner)', round(avg((winner_id = red_fighter_id)::int)::numeric, 4)::text FROM lf WHERE outcome = 'win' AND red_fighter_id IS NOT NULL
UNION ALL SELECT 'fights with a corner recorded', count(*)::text FROM lf WHERE outcome = 'win' AND red_fighter_id IS NOT NULL
UNION ALL SELECT 'last event with mdabbert odds', max(event_date)::text FROM lf WHERE has_odds
UNION ALL SELECT 'winner fights from 2026-05-17 (Wikipedia period)', count(*)::text FROM lf WHERE outcome = 'win' AND event_date >= DATE '2026-05-17'
UNION ALL SELECT 'train / val / test (rolling 365 d) winner fights',
  concat_ws(' / ', count(*) FILTER (WHERE event_date < DATE '2024-10-04'),
                   count(*) FILTER (WHERE event_date >= DATE '2024-10-04' AND event_date < DATE '2025-10-04'),
                   count(*) FILTER (WHERE event_date >= DATE '2025-10-04')) FROM lf WHERE outcome = 'win'
UNION ALL SELECT 'test fights with mdabbert odds', count(*)::text FROM lf WHERE outcome = 'win' AND event_date >= DATE '2025-10-04' AND has_odds;
```

The 406 comes from a second query, run the same way (it needs fighter ids, which the CTE above does not carry). It was run on 2026-10-14, with the database at history through 2026-10-03, and is the figure quoted in the 2b note in ROADMAP.md:

```sql
SELECT count(DISTINCT p)
FROM (
  SELECT f.fighter_a_id AS p FROM fights f JOIN events e ON e.id = f.event_id
  WHERE e.event_date >= DATE '2026-05-17' AND f.status = 'completed'
  UNION
  SELECT f.fighter_b_id FROM fights f JOIN events e ON e.id = f.event_id
  WHERE e.event_date >= DATE '2026-05-17' AND f.status = 'completed') x;
```

### Phase 2a follow-up: what the jerzyszocik daily odds dataset would add (not loaded; D-034)
- Date: 2026-10-14
- Hardware: not a timing (one run took about 3 s on the machine above); counts do not depend on it.
- Commit: `51a2b66` (the script). The database was at history through 2026-10-03.
- Command: `uv run python scripts/odds_daily_probe.py data/raw/jerzyszocik__ufc-betting-odds-daily-dataset/UFC_betting_odds.csv --expect-sha256 44fc6838e7ae9698883ac36f5238667a16576067d7b31c2f23077b3066f9f716` (add `--json` for the full report these tables are copied from). Read-only: the database connection runs `SET TRANSACTION READ ONLY` and nothing is loaded.
- Snapshot: `UFC_betting_odds.csv` from Kaggle `jerzyszocik/ufc-betting-odds-daily-dataset`, CC0, **sha256 `44fc6838e7ae9698883ac36f5238667a16576067d7b31c2f23077b3066f9f716`**, 32,650,206 bytes, created on Kaggle 2026-10-14 14:47 UTC, downloaded 2026-10-14 20:08 EDT (2026-10-15 00:08 UTC) with kaggle CLI 2.2.4 into `data/raw/jerzyszocik__ufc-betting-odds-daily-dataset/` (gitignored, with a `SNAPSHOT.json` sidecar; deliberately not in `scripts/seed_sources.json` or the seed manifest, because nothing is loaded from it). **The file is regenerated upstream every day, so this hash is the only way to reproduce these numbers.**
- Our side: 8,771 fights with a decided winner, 6,902 of them with mdabbert odds. Windows are D-031's: validation from 2024-10-04, test from 2025-10-04, Wikipedia period from 2026-05-17.
- Matching: the ufcstats id when the feed row has one, else the normalized, unordered fighter-name pair within +-1 day (the rule the existing loaders use). A row only counts if it has both moneyline prices.

**Coverage of our labeled fights**

| | fights | mdabbert | daily dataset | by ufcstats id | by name + date | adds beyond mdabbert | union |
|---|---|---|---|---|---|---|---|
| test year | 553 | 241 | 426 | 0 | 426 | **237** | 478 (86%) |
| &nbsp;&nbsp;test, before 2026-05-17 | 330 | 241 | 264 | 0 | 264 | **75** | 316 (96%) |
| &nbsp;&nbsp;test, from 2026-05-17 (Wikipedia) | 223 | 0 | 162 | 0 | 162 | **162** | 162 (73%) |
| validation | 512 | 476 | 324 | 303 | 21 | **0** | 476 (93%) |
| train | 7,706 | 6,185 | 5,971 | 5,971 | 0 | **1** | 6,186 (80%) |

By year, 2023 on (all years are in the `--json` output):

| | fights | mdabbert | daily dataset | by ufcstats id | by name + date | adds beyond mdabbert | union |
|---|---|---|---|---|---|---|---|
| 2023 | 504 | 416 | 416 | 416 | 0 | **0** | 416 (83%) |
| 2024 | 513 | 383 | 370 | 370 | 0 | **0** | 383 (75%) |
| 2025 | 516 | 500 | 331 | 213 | 118 | **1** | 501 (97%) |
| 2026 | 425 | 114 | 329 | 0 | 329 | **236** | 350 (82%) |

- **The gain is entirely in the test year**: +237 fights beyond mdabbert, of which **162 are in the Wikipedia period, where mdabbert has none**. Validation adds 0 and train 1.
- **The ufcstats id only helps up to 2025.** In the test year it matches **0** fights, because every row dated 2026 has no `fight_url`: 139,048 of 214,407 rows are blank, and so is every fighter URL on them. 2026 can only be matched by name and date.

**The 75 test fights with no odds in either source** (rule for "spelling variant": same person if the sorted name tokens or the last token agree, or the difflib ratio is >= 0.75):

| | before 2026-05-17 | from 2026-05-17 | all |
|---|---|---|---|
| spelling variant of a fight in the feed | 14 | 32 | 46 |
| one fighter matches, the other is different | 0 | 5 | 5 |
| bout not in the feed | 0 | 24 | 24 |

**Ceiling with alias review, test year (553 fights):** exact matching reaches 478 (86.4%). Resolving the spelling variants reaches **524** (94.8%); resolving every fight where one fighter matches would reach at most 529 (95.7%), but the 5 "different opponent" fights look like replacement opponents, which no alias fixes. The rule is loose and was not hand-checked fight by fight, so treat 524 as a measured estimate, not a guarantee.

**Is the price any good?** (test fights with a pre-fight consensus: last snapshot per bookmaker on or before the event day, de-vigged, each row oriented to our fighter A, averaged)

| | |
|---|---|
| fights with a consensus | 426 |
| consensus favorite won | 304 of 426 (71.4%) |
| ...on the 237 fights mdabbert lacks | 166 of 237 (70.0%) |
| agrees with mdabbert's favorite | 181 of 187 (96.8%) |
| bookmakers per fight | median 24, min 1, max 30 |
| last snapshot, days before the event | median 0; 68% within 1 day |

**What the file is.** 214,407 rows: a one-time historical import (source `zewnetrzne`, 6,299 rows for events 2010 to 2024-12-07, all added 2025-07-27, so `adding_date` is not a capture time there) plus live daily snapshots from 39 distinct bookmaker names from 2025-07-26 on (684 rows have no `adding_date` and no source). 100% of the 2026 rows were added on or before the event day. Event dates run to 2028-07-30 (upcoming placeholders).

**What this does and does not show.** It shows how many of our fights a loader could price, and that the feed's favorite agrees with mdabbert's where both exist. It does not show how the model compares with this baseline: nothing was loaded and the model was not re-scored. The consensus is "last snapshot on or before the event day", which is close to a closing line (68% within a day) but is not proven to be one.
