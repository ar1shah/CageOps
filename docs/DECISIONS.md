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

### D-008: Amend D-006, what we load from the silver file
- Context: D-006 assumed the jerzyszocik silver file was raw fight facts only. Profiling it (docs/DATA_SOURCES.md, "Silver Parquet findings") showed it also contains (a) scrape-time career stats per fighter (`SlpM`, `Str_Acc`, `TD_Def`, ... and the `w`/`l`/`d` record) filled for every fight, (b) odds columns with no capture date, and (c) a `ranking` column with no as-of date.
- Options considered:
  1. Load everything silver offers and use it.
  2. Load only fight facts and per-round stats from silver, and treat the rest as untrusted.
  3. Drop silver and rebuild everything from scraping.
- Decision: option 2. Point-in-time correctness means a feature for a fight may only use what was known strictly before that fight. A career stat scraped in 2026 includes every fight a fighter had after 2019, so using it for a 2019 fight lets the model see the future (the answer is baked into the input). Silver's odds and rankings have no capture date, so we can't prove they pre-date the fight. Therefore: (a) silver's career stats and record are not loaded as features; the feature pipeline computes everything from per-round facts, (b) silver's odds are loaded into the `odds` table with `source='silver'` and a null `captured_at`, next to mdabbert's (`source='mdabbert'`); mdabbert is the primary baseline because it is built from bestfightodds and meant to be pre-fight, and silver only fills gaps if the two agree on the favorite at a high rate (measured in the Phase 1a plan), (c) silver's `ranking` column is ignored; rankings come from `UFC_rankings_history.csv` with an as-of join (each fighter's latest rank strictly before the fight date). Odds are never model features (baseline only), per D-006.
- Tradeoff: more loaders and a source column to carry through, and we discard columns that would have been convenient. In exchange the leakage test has something real to prove. Revisit if a source documents when its odds or rankings were captured.

### D-009: Fight schema conventions (order, corner, cleaning)
- Context: silver puts the winner in slot 1 in 64% of fights (slot 1 is the red corner, and red wins 64.2% in the loaded data), so slot order carries information. Silver also has messy values: whitespace-padded results, the literal string "NULL", title names in the weight-class column.
- Options considered:
  1. Keep silver's f_1/f_2 order as row order.
  2. Store fighters in a neutral order and drop corner entirely.
  3. Store fighters in a neutral order and keep corner as a separate, nullable raw fact.
- Decision: option 3. `fights.fighter_a_id < fighter_b_id`, with fighter ids assigned in ufcstats-id order at seed time, so row order is unrelated to the outcome (checked on real data: winner is fighter_a in 50.1% of fights, red is fighter_a in 50.3%). The winner is `winner_id`, never "A/B". Corner is `red_fighter_id` (nullable): it is assigned before the fight, so it is a legitimate pre-fight fact, and the leak was only ever slot order used as row order. The loader stores raw facts; Phase 1c decides whether corner becomes a feature. ufcstats-derived data (silver) is canonical for corner and winner, and only silver creates rows in `fights`; mdabbert is cross-checked against it (6 red-corner and 2 winner disagreements are logged in the load report; the two winner disagreements were checked by hand against the official result and are mdabbert errors: Davis beat Jones by unanimous decision, and Jandiroba beat Martin) and its 14 unmatched fights (e.g. Hall vs Souza, cancelled) are reported and skipped. Cleaning rules, each covered by a table-driven test over every observed raw value, where an unknown value fails the load: whitespace collapsed in `result`; Doctor's Stoppage maps to `ko_tko` with the detail kept in `method_detail`; a bare "Decision" takes its type (unanimous/split/majority) from the details column; scheduled rounds of 0 become NULL; the literal strings "NULL" and "" become NULL; `weight_class` is nullable (10 early fights and one "Nieznana" are unknown); 17 recent title fights that carry the title name in the weight-class column are mapped to their division with `is_title_fight` set; fights with no per-round data (21, all from 1994 to 1998) get `has_round_stats = false` and no rows, never zeros.
- Tradeoff: fighter ids follow ufcstats-id order only for the seed fighters, so fighters added later by the scraper get larger ids and `fighter_a` is then "the seed fighter" more often than chance (training swaps order randomly, so this is cosmetic). Revisit if a model ever uses row order. Silver's red-corner inference (slot 1 = red) is confirmed by mdabbert in 7,154 of 7,160 matched fights.

### D-010: Name resolution, source-scoped aliases, and matching rules
- Context: odds and rankings identify fighters by name only, and names differ between sources ("Kai Kara-France" vs "Kai Kara France", "Bobby Green" vs "King Green"). Silver has 2,686 fighters but one name that belongs to two people (Bruno Silva, a middleweight and a flyweight).
- Options considered:
  1. Exact string match, drop the rest.
  2. Fuzzy matching that picks the closest name.
  3. Normalized-name match, evidence-based reviewed aliases scoped per source, and never guessing when ambiguous.
- Decision: option 3. Names are normalized (accents stripped, punctuation dropped). A name matching more than one fighter is narrowed by weight class and active dates, and if still not unique it is left unmatched and listed; it is never guessed (tested with two same-name fighters). Aliases are keyed `(source, alias_norm)`, so an alias reviewed for mdabbert rows never resolves a name from rankings, where "Tim Johnson" or a bare "Derrick" could be someone else. The 124 aliases are committed in `aliases.csv`: 96 for mdabbert, each backed by an opponent-and-date match and reviewed by hand, and 28 for the two rankings sources. Rankings have no opponent to check, so a reviewed alias there needed a rule: the candidate must match the ranked division and have fought within about 12 months of every snapshot where the name appears (this accepted `Tim Johnson` as Timothy Johnson, a heavyweight ranked in 48 snapshots, and `Luis Henrique Barbosa` as Luis Henrique). Two candidates were rejected by hand as different people: `Jaime Alvarez` (only similar to Joel Alvarez) and the closest spelling for `Antonio Rogerio Nogueira`, which is Minotauro (a heavyweight) when the ranked fighter is Rogerio Nogueira, "Lil Nog", a light heavyweight. mdabbert rows match a fight on exact date first, then +/-1 day for cards that cross midnight; on the real data 7,160 of 7,177 rows (99.76%) matched on the exact date and none needed the fallback. Unmatched rows are kept with a null fighter and listed in the load report.
- Tradeoff: every new source needs its own reviewed alias list, and unreviewed candidates stay unloaded. Remaining misses: 3 mdabbert names (a typo, a fighter missing from silver, and one ambiguous Bruno Silva fight), 14 fights mdabbert has that silver does not (reported and skipped), and 3 ranking names (0.51% of jerzyszocik's, 0.32% of martj42's). Revisit if a source ships fighter ids.

### D-011: Rankings sources, snapshot quality, and the as-of rule
- Context: `UFC_rankings_history.csv` (jerzyszocik) is clean through 2025-07-27, but all 63 weekly snapshot dates from 2025-08-03 to 2026-10-01 contain two lists merged under one date (a fighter appears at two ranks, e.g. 28 rows for 17 fighters in Women's Bantamweight on 2026-10-01), and the file is sorted by rank so the lists can't be separated. Snapshot dates also fall on different weekdays across eras (Monday, Tuesday, Sunday, Thursday), so they look like the author's scrape schedule, not the UFC's publish date. Across 70 title changes, the new champion first appears 2 to 30 days after the event and never on or before it.
- Options considered:
  1. Load everything with a quality flag and filter later.
  2. Load only clean snapshots, and fill the gap from a second source if one is clean.
  3. Scrape the official rankings page ourselves for the missing period.
- Decision: option 2. A snapshot date is skipped entirely if any fighter appears twice in one list (the raw file stays on disk and in the manifest). martj42/ufc-rankings (CC0) is loaded as a second source: 530 clean dates through 2026-06-02 (2 dates skipped for the same reason). The view `fight_pre_rankings` uses only snapshots dated strictly before the fight, returns a NULL rank (status `stale`) if the latest snapshot is more than `RANKING_MAX_AGE_DAYS = 21` days old (a named constant with a test that checks the view definition), and prefers a fresh jerzyszocik snapshot over a fresh martj42 one. Snapshot dates that are late only make ranks staler, never leak, because a scrape date is never earlier than the publish date: that is the safe direction for a strictly-before join. Other rules: vacant-title rows (empty or "NA" names) are skipped, and the bare "Pound-for-Pound" list, which runs 2013-02-04 to 2020-01-20 before women's pound-for-pound existed, is treated as the men's list.
- Tradeoff: no ranking data for fights after about 2026-06-23 (martj42 ends 2026-06-02, plus the 21-day cap), which includes every upcoming fight we want to predict, so Phase 1b needs a live rankings source. The two sources disagree on about 5% of rows for the same older dates, so the view never mixes them within one snapshot. Revisit when a live source exists.

### D-012: Integration tests run against real Postgres, including in CI
- Context: the loaders rely on `INSERT ... ON CONFLICT`, CHECK constraints, a SQL view, and `CREATE EXTENSION vector`, which SQLite doesn't behave like.
- Options considered:
  1. SQLite in-memory for tests.
  2. Mock the database layer.
  3. A real Postgres (the same pgvector image as local dev) for integration tests, locally and as a CI service container.
- Decision: option 3. Tests build a separate `cageops_test` database from the Alembic migrations (so migrations are exercised on every run) and truncate tables between tests. Locally they skip with a clear reason if Postgres is unreachable; in CI `REQUIRE_DB=1` makes an unreachable database a failure so tests can't be silently skipped. Fixtures are tiny and built inside the tests; the full-dataset load stays local, because `data/` isn't in CI.
- Tradeoff: CI takes longer and needs the Postgres service, and tests share one database so they can't run in parallel. Revisit if the suite grows slow (per-worker databases).

### D-013: ufcstats.com bot gate: never bypass, build against fixtures and a replay server
- Context: on 2026-10-03 (20:17 UTC, User-Agent `CageOps/0.1 (+https://github.com/ar1shah/cageops)`, 3 requests) `http://ufcstats.com/robots.txt` still returned 404 (no rules, no Crawl-delay), but the homepage and `/statistics/events/completed` both returned 200 with a "Checking your browser" page instead of content. The page runs a proof-of-work script (find `n` so that `sha256(nonce:n)` starts with `00`, POST it to `/__c`, reload) and carries `<meta name="robots" content="noindex">`. On 2026-10-02 only a fight page was gated; now every content page is, so the footer (and any terms) couldn't be read. The Kaggle silver file is still dated 2026-06-10, so no compliant source exists for June to October 2026 fight stats.
- Options considered:
  1. Solve the challenge in code, or run a headless browser that solves it.
  2. Pause Phase 1b until the operator replies.
  3. Build the whole pipeline now against hand-saved fixtures and a local replay server, treat the challenge as a stop signal, and ask the operator for access.
- Decision: option 3. The gate is the operator saying "no automated clients", and robots.txt being absent doesn't override it. Solving it would be circumventing an access control, which also weakens the public-pages argument D-004 relies on (*hiQ* covers pages that are open, not pages behind a barrier we got around). Rules, permanent: no headless browsers, no challenge solvers, no proxy or User-Agent rotation, ever. The fetcher treats a challenge page (or a 403) as a circuit breaker that stops every worker, is never retried and never cached, and stays open until a human resets it (D-016). The fetcher goes through a `Source` interface so another source plugs in without touching the queue, cache, rate limiter or DLQ. Benchmarks run against a local replay server and are labeled as such, never as live-site numbers. Ari emails the operator asking to allowlist the CageOps User-Agent. Phase 1d (ROADMAP.md) adds a results source that permits automated access to close the June to October gap and support Phase 5 grading.
- Tradeoff: no live June to October data until the operator agrees or Phase 1d lands, and the Phase 1b "backfill 12 months" check is proven against replayed pages rather than the live site. Revisit if the operator replies, the gate comes down (a manual check from a browser, not a probe loop), or Phase 1d finds a source.

### D-014: Global rate limit as an atomic next-slot reservation in Redis
- Context: CLAUDE.md rule 2 caps us at about 1 request/second against ufcstats, shared across every worker. Workers are separate processes (and later separate pods), so the limit has to live somewhere they all see: Redis.
- Options considered:
  1. Read the "next allowed time" key, check it, write it back (GET then SET). This is a race: two workers read the same value in the same millisecond, both see a free slot, both write, and both fetch at once. Reproduced under 8 threads: 26 of 40 slots were double-booked.
  2. A token bucket in a Lua script. Correct, but it allows bursts (we don't want any), keeps two pieces of state (tokens and last refill) and needs refill math, and a caller without a token has to poll and come back.
  3. A Lua script that reserves the next slot: read `next_allowed`, take `max(now, next_allowed)` as my slot, store `slot + interval`, return the slot and the wait. The caller sleeps until its slot.
  (A fixed-window counter was also rejected: it allows two requests 1 ms apart across a window boundary, so it limits volume, not spacing. WATCH/MULTI works too but retries in a loop under contention.)
- Decision: option 3. Redis runs one script at a time, so the read and the write can't be interleaved. It is about 8 lines, first come first served, with no polling. Time comes from Redis `TIME` inside the script, not the worker's clock, so containers with drifting clocks still agree. The script returns the absolute slot (ms) as well as the wait, so the concurrency test asserts on slots (all distinct, at least one interval apart) and can't flake on client timing. One key per source (`ratelimit:{source}`), so the replay source has its own limit. A 429 with `Retry-After` calls `penalize()`, which moves the next slot out for all workers. If Redis is down no slot can be taken, so the fetcher sends nothing (fails closed).
- Tradeoff: every request costs a Redis round trip, which is negligible at 1 req/s. A worker waiting for its slot holds its job while it sleeps, so many workers all queue behind one limiter and add nothing: that is the bottleneck the Phase 1b benchmark measures. Spacing is per request, not per second, so we never burst even after being idle. Redis eviction of the key would reset the limit, so production Redis needs `maxmemory-policy noeviction`. Revisit if a source allows bursts (then a token bucket fits) or a second limit tier is needed (per path).

### D-015: Raw HTML cache policy by page type, with the replay source kept separate
- Context: CLAUDE.md says never re-fetch a cached page unless forced. But some pages change: a card gets bouts added or cancelled, results post on fight night, stats are sometimes corrected the next day, fighter bios get edited. "Cache forever" would freeze wrong data; "never cache" would break politeness and the benchmark.
- Options considered:
  1. Cache everything forever; `--force` to refresh.
  2. A single TTL for every page.
  3. A freshness rule per page type (a Source method), with finished events and fights treated as immutable once they have had time to settle.
- Decision: option 3, stored in `raw_pages` (one row per URL, latest copy, only 200 and 404 are kept). Fight and event pages: fresh for 6 hours, and final for good once fetched 3 or more days after the event date (the job always knows the event date, which also comes from the event page, not the fight page). Event date unknown: 6 hours. Fighter pages: 30 days (we keep only bio fields). Completed events list: 12 hours. Upcoming events list: 6 hours. robots.txt: 24 hours (RFC 9309's maximum). Any 404: 1 day (the page may appear later). Server errors, 429s and bot-challenge pages are never cached: caching them would hide the real page behind a bad copy. `--force` skips the cache read only; it still obeys the circuit breaker and rate limiter and still writes the new copy. A cache hit returns before the rate limiter, so a warm run never waits on it. The replay server (checkpoint 5) is its own source name, `ufcstats_replay`, with its own base URL from env: its pages are stored with `source='ufcstats_replay'` and are never served to the real source, it has its own limiter key (`ratelimit:ufcstats_replay`) and breaker key (`breaker:ufcstats_replay`), and the code refuses to pair the name `ufcstats` with a non-ufcstats host (or the replay name with the real host), so benchmark data can't be filed as live data.
- Tradeoff: a stats correction posted more than 3 days after an event is missed unless someone forces a refetch. 3 days is a guess; revisit if the checkpoint 3 seed-versus-scrape comparison shows late corrections. Storing full HTML in Postgres costs space (about 60 KB a page, so roughly 100 MB for the whole history) in exchange for being able to re-parse without the network, which the warm benchmark relies on. Revisit if the table grows large (object storage keyed by hash).

### D-016: robots.txt handling, error classes, and a manual circuit breaker
- Context: the scraper must check robots.txt on startup and refuse to run if a path it needs is disallowed. Separately, it must tell errors worth retrying from ones that aren't, and it must never hammer a site that has just blocked it (D-013).
- Options considered:
  1. Treat a missing or unreadable robots.txt as "allowed".
  2. Follow RFC 9309: 4xx means no rules, 5xx or unreachable means assume disallowed.
  3. For the block signal: let the normal retry logic handle it, or auto-reset the breaker after a cooldown, or require a human to reset it.
- Decision: option 2 for robots.txt (today ufcstats returns 404, which means no rules; our own 1 req/s limit still applies). A `Crawl-delay` longer than our default replaces the default; we parse it ourselves because the standard library only reads whole numbers, and the most specific group wins with no inheritance from `*`. The check runs through the same Fetcher (rate limited, cached 24 hours) at every start. Error classes: timeouts, connection errors, 5xx and 429 are retryable (429 also backs every worker off); 404 and other 4xx, cross-site or over-long redirects and parse errors are permanent; a challenge page or 403 is neither: it opens a circuit breaker that stops all workers, is never cached or retried, and has no timer. A person closes it after checking what the site is doing (`reset`, with a CLI command in checkpoint 4). Same-site redirects (including http <-> https on ufcstats.com) are followed by running the whole fetch path again on the `Location` URL, so every hop goes through the cache, the breaker and the rate limiter and costs its own slot; at most 3 hops, then a permanent error (this also ends redirect loops). A redirect to another host is a permanent error and the other host is never contacted, because a hop that left the site would skip the robots and rate-limit checks. The `Location` is logged on every 3xx. After a followed redirect the final page is also filed under the originally requested URL, so asking for it again is a cache hit rather than another round trip. The breaker is checked before taking a rate-limit slot and again after waiting for it, so a worker queued behind the one that tripped it sends nothing.
- Tradeoff: a human has to notice and reset the breaker (a metric, `scraper_source_blocked_total`, is there for alerting in Phase 6), so a false positive stops ingestion until someone looks. We accept that: probing a site that told us to go away is the thing we promised not to do. Treating other 4xx on robots.txt as "no rules" follows the RFC but is permissive; revisit if a source's 403 on robots.txt should instead count as a block (today 403 already trips the breaker).

### D-017: Fixtures: full pages stay local, only mechanically minimized copies are committed
- Context: parser tests need real ufcstats HTML (CLAUDE.md: test against saved fixtures, never live requests). But DATA_SOURCES.md section 5 says to "store facts (numbers, results), not page layouts; don't redistribute raw HTML", and the repo is public. Committing 12 whole pages, with their scripts, styles, navigation and ads, would be redistributing the site's pages.
- Options considered:
  1. Commit the full hand-saved pages.
  2. Hand-write synthetic HTML that imitates the structure. Nothing is redistributed, but it only tests what we guessed the markup looks like, which is the thing the tests are meant to check.
  3. Keep the full pages in `data/fixtures/ufcstats/` (gitignored) and commit only copies with everything the parsers don't read deleted, by a script that can only delete.
- Decision: option 3. `scripts/minimize_fixture.py` finds where scripts, styles, noscript, iframes, ad slots (`<ins>`), inline SVG, `<nav>`, `<footer>`, `<link>` tags and comments start and end in the saved text and cuts those ranges out. It never re-serializes: every remaining character is exactly as saved (tags, attribute order and quoting, classes, entities, whitespace, line endings), so the fixture is still what the scraper would really receive. It checks that the output is the input with deletions only, refuses a bot-challenge page, refuses an unclosed element rather than guessing, and never overwrites its input. More can be removed with `--remove-tag/--remove-class/--remove-id` once the parsers show what they never read. Only the minimized files go to `services/scraper/tests/fixtures/ufcstats/`.
- Tradeoff: the committed files still hold the facts we need (names, numbers, results) and the tag and class structure of the parts we parse, so this reduces redistribution to the minimum the tests need but does not eliminate it. It is a small number of pages, factual content, kept for testing, but it is a judgment call. If the operator objects (the email asked for permission), replace them with synthetic fixtures built from the same structure, or drop the committed copies and keep them local. Because the full pages are not in git, a fresh clone can't regenerate the minimized ones; the minimized files are the source of truth in the repo, and the full pages are Ari's local copy.

### D-018: Parsers: pure functions that return the page's own words, with loose layout checks
- Context: Phase 1b turns ufcstats HTML into rows that line up with the Phase 1a seed. The pages have traps (see DATA_SOURCES.md, "ufcstats page structure"): the event page lists the winner first, the fight page lists the red corner first and has no date, the per-round table nests `<thead>` rows inside `<tbody>`, the totals header has a duplicated "Td %", the fighter page mixes bio facts with "as of today" career rates, and the "completed" events list starts with the next upcoming event.
- Options considered:
  1. Parse straight into database rows (normalize method, weight class, gender inside the parser).
  2. Parsers return what the page says in the site's vocabulary as typed pydantic models; a separate step maps it to our vocabulary using the Phase 1a functions.
  3. Regex over the raw HTML, no HTML library.
  For the HTML library: BeautifulSoup with lxml, BeautifulSoup with the built-in `html.parser`, or selectolax.
- Decision: option 2, with BeautifulSoup + lxml. Parsers are pure (`parse_*(html, url) -> model`, no network, no database, no logging); the URL is a plain argument because event and fight pages don't contain their own id. Mapping to `decision`/`unanimous`, canonical weight class and gender happens in checkpoint 3 with `normalize_result` and `fight_weight_class`, so scraped and seeded rows go through the same code. BeautifulSoup keeps document order on the malformed per-round table (tested with both backends), so that table is read in order ("Round 1", row, "Round 2", row, ...) rather than by nesting; lxml because the warm-cache benchmark re-parses every page and is CPU-bound (measured in checkpoint 5), and it ships wheels. selectolax's default backend is deprecated and failed to import. Rules baked into the models: a blank cell ("--", "---", empty) is None and a published "0 of 0" stays 0; an unknown value format raises instead of guessing; the fight model has no date field (the event page is the only source of a fight's date); the fighter model has exactly four bio fields and no career rates or record (a test pins the field list); event-page fighter order is documented as winner-first and never used as a corner or a column order, and only the fight page (first fighter, confirmed against the seed's red corner in 6 of 6 fixtures) can supply the red corner. Layout checks are deliberately loose: required landmarks must exist and each table must have the expected column count plus a few key headers (lowercased, punctuation removed, "contains" tests at fixed positions), and cells are read by position, so a cosmetic fix on the site (the "Td %" typo, capitalization, punctuation) changes nothing, while a removed column or missing landmark raises `ParseError` (permanent, never retried, goes to the DLQ with the URL). Suspicious values that don't make a page unreadable (per-round significant strikes not adding up to the total, an unrecognized time format) are returned in `anomalies` on the model; `report_anomalies()` logs a WARNING and counts `scraper_parse_anomaly_total{source,kind}`, called by the jobs, so the parser stays pure and the page still loads.
- Tradeoff: a markup change that keeps the column count and the checked headers but reorders other columns would parse wrong values, not fail; the per-round sum check and the cross-check between the event page's winner and the fight page's W/L badge (a test now, an ingestion check in checkpoint 3) are the backstops. Only a few headers are checked, so some cosmetic fixes pass and some real changes slip through. Mapping to our vocabulary lives in a second step, so there are two places to look when a value is wrong. Revisit if a parse anomaly shows up in production often (tighten the checks), or if parsing speed matters at scale (the warm benchmark will say).
