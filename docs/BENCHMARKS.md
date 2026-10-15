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
