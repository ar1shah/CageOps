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
