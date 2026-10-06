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


_None yet. First benchmarks arrive in Phase 1b (worker scaling)._
