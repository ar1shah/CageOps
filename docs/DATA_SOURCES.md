# CageOps Data Sources

_Researched: 2026-10-01. Re-verify anything marked **[UNVERIFIED]** before relying on it. This is not legal advice._

## TL;DR

| Source | Role in CageOps | Access | Biggest risk |
|---|---|---|---|
| ufcstats.com | Live fight / fighter stats (Phase 1b scraper) | Public HTML, no API, no published terms found | robots.txt could not be retrieved for this audit; check it at scraper start |
| Kaggle: `jerzyszocik` silver data | Historical seed (raw per-round stats) | Download, CC0 | Last refreshed 2026-06-10 despite "weekly" claim |
| Kaggle: `mdabbert` Ultimate UFC | Closing odds + rankings for the betting-favorite baseline | Download, CC BY 4.0 | No per-round stats; license differs between Kaggle and GitHub |
| Kaggle: `martj42` rankings | Historical rankings (2013+) | Download, CC0 | Rankings are media votes, weekly-ish |
| ESPN MMA endpoints | **Do not use** | Undocumented JSON | Disney Terms of Use explicitly ban scraping and ML use |
| News RSS (Sherdog, MMA News, Cageside Press) | Phase 4 RAG corpus | Public RSS | Site terms restrict copying/aggregation; store excerpts + links only |

---

## 1. ufcstats.com

### 1.1 What it is

ufcstats.com is the public stats site built on FightMetric data. FightMetric became the UFC's official statistics provider in September 2010 ([FightMetric blog](http://blog.fightmetric.com/2010/09/fightmetric-now-official-statistics.html); [Bloody Elbow](https://bloodyelbow.com/2010/09/02/fightmetric-to-be-the-ufcs/)). The Rajeev Warrier Kaggle dataset notes that ufcstats "came into picture" after FightMetric's own site ceased to exist ([Kaggle API metadata, rajeevw/ufcdata](https://www.kaggle.com/api/v1/datasets/view/rajeevw/ufcdata)). Legacy FightMetric URLs with the same ID scheme still appear in search results ([hosteddb.fightmetric.com example](http://hosteddb.fightmetric.com/fighter-details/48a9a128784d53d1)).

Pages are served over plain HTTP with 16-character hex IDs, e.g. `http://ufcstats.com/fighter-details/032cc3922d871c7f` ([example fighter page](http://ufcstats.com/fighter-details/032cc3922d871c7f)).

### 1.2 robots.txt (verified 2026-10-01 and 2026-10-03)

**Update 2026-10-03:** `http://ufcstats.com/robots.txt` returns `404 Not Found` (checked 2026-10-01 and again 2026-10-03), so there are no crawl rules and no Crawl-delay. But every content page (homepage, events list, fight pages) now returns a "Checking your browser" proof-of-work challenge instead of content, so the footer couldn't be read either. We don't bypass it; see D-013. The original audit notes follow.

I could not retrieve `ufcstats.com/robots.txt` during this audit: both the `http://` and `https://` fetches failed at the connection level from my research environment, so **I cannot tell you its current contents**. I did not try to work around that.

What I could find:

- A third-party SEO checker reports "Robots.txt file not found" and no XML sitemap for ufcstats.com ([sitescorechecker](https://ufcstats.com.sitescorechecker.com/)). That snapshot is undated and lists a domain expiry of 2021, so treat it as stale.
- For comparison, the parent brand's `ufc.com/robots.txt` sets `Crawl-delay: 15` for all user agents and blocks admin, login, and search paths ([ufc.com/robots.txt](https://www.ufc.com/robots.txt)). This file does **not** govern ufcstats.com, but it signals how Zuffa thinks about crawl rate.

**Action for Phase 1b (already in the roadmap):** the scraper must fetch and log robots.txt on startup and refuse to run if a relevant path is disallowed. Manual check from your machine:

```bash
curl -sS -i http://ufcstats.com/robots.txt
```

Interpretation: a `404` means no robots rules (allowed by convention, but your ~1 req/s limit still applies). A `Crawl-delay` above 1 second overrides the CageOps default.

### 1.3 Terms of use

I found **no terms-of-use page** for ufcstats.com in search results, and none of the scraper projects I reviewed cite one ([Greco1899/scrape_ufc_stats](https://github.com/Greco1899/scrape_ufc_stats); [Crawlbase guide](https://crawlbase.com/blog/best-way-to-scrape-ufc-stats/); [Apify automation-lab actor](https://apify.com/automation-lab/ufcstats-scraper)). The Crawlbase guide's advice is to "read the terms of service and the robots.txt of whatever site you point this at, including ufcstats.com, and treat both as the boundary for what you collect and how fast" ([Crawlbase](https://crawlbase.com/blog/best-way-to-scrape-ufc-stats/)). **[UNVERIFIED]**: confirm by scrolling to the footer of the live site.

Absence of terms is not permission. The UFC is a subsidiary of TKO Group Holdings ([Wikipedia: TKO Group Holdings](https://en.wikipedia.org/wiki/TKO_Group_Holdings)); the brand historically sat under Zuffa ([Wikipedia: Zuffa](https://en.wikipedia.org/wiki/Zuffa)).

### 1.4 Page types and fields

URL patterns ([Apify automation-lab](https://apify.com/automation-lab/ufcstats-scraper); [Greco1899 scraper library](https://github.com/Greco1899/scrape_ufc_stats/blob/main/scrape_ufc_stats_library.py)):

| Page | URL pattern |
|---|---|
| Completed events list | `http://ufcstats.com/statistics/events/completed?page=all` |
| Event detail | `http://ufcstats.com/event-details/{id}` |
| Fight detail | `http://ufcstats.com/fight-details/{id}` |
| Fighter profile | `http://ufcstats.com/fighter-details/{id}` |
| Fighter A-Z index | `http://ufcstats.com/statistics/fighters?char={a-z}&page=all` |

**Event list.** One row per event: name, date, location ([Greco1899](https://github.com/Greco1899/scrape_ufc_stats/blob/main/scrape_ufc_stats_library.py); [Apify automation-lab](https://apify.com/automation-lab/ufcstats-scraper)). Coverage runs from UFC 2 (1994-03-11) through upcoming cards, ~788 events at the time Apify's actor documented it ([Apify neverempty](https://apify.com/neverempty/ufc-stats-scraper)).

**Event detail.** Event name, date, location, and the bout list: both fighters, winner, weight class, method, round, time, plus summary knockdowns, significant strikes, and takedowns per fighter ([Apify automation-lab](https://apify.com/automation-lab/ufcstats-scraper)).

**Fight detail** (the richest page) ([Greco1899](https://github.com/Greco1899/scrape_ufc_stats/blob/main/scrape_ufc_stats_library.py); [Apify neverempty](https://apify.com/neverempty/ufc-stats-scraper)):

- Result per fighter (win / loss / draw / no contest), weight class, title-fight flag
- Method, finish details, round, time, time format, referee
- Judges' names and scores for decisions
- **Totals**, overall and **per round**: knockdowns, significant strikes landed/attempted and %, total strikes, takedowns landed/attempted and %, submission attempts, reversals, control time
- **Significant-strike breakdown**, overall and **per round**: head / body / leg, and distance / clinch / ground

**Fighter profile** ([Greco1899](https://github.com/Greco1899/scrape_ufc_stats/blob/main/scrape_ufc_stats_library.py); [Apify neverempty](https://apify.com/neverempty/ufc-stats-scraper); [Apify automation-lab](https://apify.com/automation-lab/ufcstats-scraper)):

- Name, nickname, record (W-L-D, NC shown in page title, e.g. "Tom Aspinall Record: 15-3-0 (1 NC)" per [search listing](http://ufcstats.com/fighter-details/399afbabc02376b5))
- Tale of the tape: height, weight, reach, stance, date of birth
- Career rates: SLpM (sig. strikes landed per minute), SApM (absorbed per minute), striking accuracy and defense, takedown average, accuracy and defense, submission average
- Fight history table: opponent, event, date, method, round, time

**Data quirks to design for** ([Apify neverempty](https://apify.com/neverempty/ufc-stats-scraper)):

- Missing values should be `NULL`, not `0`. A real zero means the site published zero.
- Fight pages reached directly may lack the event date; get it from the event page. (This matters for point-in-time correctness: the fight date must come from the event.)
- **Career rates on the fighter profile are "as of today."** Never use them as features for past fights. Recompute from fight-level rows (rule 1 in CLAUDE.md).

---

## 2. Kaggle UFC datasets

All metadata below is from Kaggle's public dataset API, fetched 2026-10-01. Kaggle licenses are **declared by the uploader**. Every dataset here is itself scraped from ufcstats.com or odds sites, so the uploader's license covers their compilation, not necessarily the underlying data.

### 2.1 Comparison

| Dataset | Covers | Date range | Last updated | License | Per-round stats | Odds | Rankings |
|---|---|---|---|---|---|---|---|
| [jerzyszocik / UFC Data: Stats & Rankings & Betting Odds](https://www.kaggle.com/datasets/jerzyszocik/ufc-fight-forecast-complete-gold-modeling-dataset) | "Silver" raw UFCStats facts + "Golden" modeling features | UFCStats history (exact range not stated) | 2026-06-10 | CC0 | **Yes** (silver) | Yes (golden) | Yes (golden) |
| [mdabbert / Ultimate UFC Dataset](https://www.kaggle.com/datasets/mdabbert/ultimate-ufc-dataset) | One row per bout, pre-fight averages, odds, ranks, plus `upcoming.csv` | 2010 onward | 2026-04-01 | CC BY 4.0 | No | **Yes** (moneyline + method odds) | **Yes** |
| [rajeevw / UFC-Fight historical data](https://www.kaggle.com/datasets/rajeevw/ufcdata) | Every UFC fight, red/blue corner stats | 1993 to 2021-03-21 | 2021-03-21 | CC0 | No | No | No |
| [jerzyszocik / UFC Betting Odds (Daily)](https://www.kaggle.com/datasets/jerzyszocik/ufc-betting-odds-daily-dataset) | Daily odds snapshots by bookmaker | "Many years" (not stated) | 2026-10-01 | CC0 | n/a | **Yes** | No |
| [martj42 / UFC Rankings](https://www.kaggle.com/datasets/martj42/ufc-rankings) | Historical official rankings | 2013 to present | 2026-06-06 | CC0 | n/a | No | **Yes** |
| [jerzyszocik / UFC rankings history](https://www.kaggle.com/datasets/jerzyszocik/ufc-rankings-history) | Historical rankings, weekly auto-update | Feb 2013 to present | 2026-09-24 | CC0 | n/a | No | **Yes** |
| [asaniczka / UFC Fighters' Statistics](https://www.kaggle.com/datasets/asaniczka/ufc-fighters-statistics) | Fighter-level career stats only (no fights) | Snapshot | 2024-02-17 | ODC-By | No | No | No |

### 2.2 Details and citations

**jerzyszocik / UFC Data: Stats & Rankings & Betting Odds.** Two files: `full_data_silver_plus.parquet` (2.6 MB) and `ufc_features.parquet` (49.8 MB), both dated 2026-06-10 ([Kaggle file list](https://www.kaggle.com/api/v1/datasets/list/jerzyszocik/ufc-fight-forecast-complete-gold-modeling-dataset)). The author describes silver as "raw fight facts straight from UFCStats: fighter bios, event details, per-round striking & grappling stats, control time, knockdowns, submissions, takedowns," and golden as adding "rolling stats (3 to 15 fights) ... rankings, betting odds and head-to-head feature differences." License CC0, version 12, last updated 2026-06-10. The description claims it is "refreshed weekly," which the update date contradicts ([Kaggle metadata](https://www.kaggle.com/api/v1/datasets/view/jerzyszocik/ufc-fight-forecast-complete-gold-modeling-dataset)). Column names were not exposed by the API; verify per-round columns when you open the Parquet in Phase 1a.

**mdabbert / Ultimate UFC Dataset.** "Merging All Kaggle Public UFC Datasets." Sources: ufcstats.com (stats), bestfightodds.com (odds), and martj42's rankings dataset. License CC BY 4.0, version 181, last updated 2026-04-01 ([Kaggle metadata](https://www.kaggle.com/api/v1/datasets/view/mdabbert/ultimate-ufc-dataset)). Files: `ufc-master.csv` (~3.2 MB) and `upcoming.csv` ([Kaggle file list](https://www.kaggle.com/api/v1/datasets/list/mdabbert/ultimate-ufc-dataset)). The header includes `R_odds`, `B_odds`, method odds (`r_dec_odds`, `r_sub_odds`, `r_ko_odds`, ...), per-division rank columns, `R_match_weightclass_rank`, and career **averages** like `R_avg_SIG_STR_landed`, but no per-round columns ([raw ufc-master.csv on GitHub](https://raw.githubusercontent.com/shortlikeafox/ultimate_ufc_dataset/master/ufc-master.csv)). A CMU capstone using it describes 6,478 fights from 2010 to 2024 with 118 features ([CMU Statistics capstone](https://www.stat.cmu.edu/capstoneresearch/fall2024/315files_f24/team11.html)). **License mismatch:** the mirror GitHub repo is labeled Apache-2.0 ([shortlikeafox/ultimate_ufc_dataset](https://github.com/shortlikeafox/ultimate_ufc_dataset)). Download from Kaggle and attribute under CC BY 4.0 to be safe.

**rajeevw / UFC-Fight historical data.** "A list of every UFC fight in the history of the organisation," scraped from ufcstats with BeautifulSoup; CC0; last updated 2021-03-21 ([Kaggle metadata](https://www.kaggle.com/api/v1/datasets/view/rajeevw/ufcdata)). Rows are per fight, where each corner has "the compiled average stats of all the fights except the current one"; no odds or rankings are documented ([WarrierRajeev/UFC-Predictions](https://github.com/WarrierRajeev/UFC-Predictions)). Five years stale.

**jerzyszocik / UFC Betting Odds (Daily).** Moneyline and method-of-victory odds with bookmaker and region, appended daily by a scheduled notebook; CC0; version 255; last updated 2026-10-01 ([Kaggle metadata](https://www.kaggle.com/api/v1/datasets/view/jerzyszocik/ufc-betting-odds-daily-dataset)).

**martj42 / UFC Rankings.** Rankings "from their inception in 2013 up until today," published "weekly, more or less, by the UFC and compiled via a vote by media members"; CC0; last updated 2026-06-06 ([Kaggle metadata](https://www.kaggle.com/api/v1/datasets/view/martj42/ufc-rankings)).

**jerzyszocik / UFC rankings history.** Rankings "since February 2013 ... updated automatically every week"; CC0; last updated 2026-09-24 ([Kaggle metadata](https://www.kaggle.com/api/v1/datasets/view/jerzyszocik/ufc-rankings-history)).

**asaniczka / UFC Fighters' Statistics.** Fighter-level records, physical attributes, and career stats; ODC-By; last updated 2024-02-17 ([Kaggle metadata](https://www.kaggle.com/api/v1/datasets/view/asaniczka/ufc-fighters-statistics)). Not useful as seed: career snapshots leak future info.

### 2.3 Recommendation

**Seed stats with jerzyszocik's _silver_ file. Use mdabbert only for odds and as a cross-check. Use jerzyszocik rankings history for rankings.**

Why:

1. **Raw facts, not features.** Silver has per-round fight facts, which is exactly what the ufcstats scraper will produce. Loading the same shape from both means one schema and a clean test: "seed rows and scraped rows for the same fight match." mdabbert and rajeevw ship **pre-computed averages**. You can't prove those are point-in-time correct, and CLAUDE.md rule 1 requires you to prove it.
2. **Do not load the "golden" features.** Same reason. Your Phase 1c pipeline builds features; using someone else's rolling stats would make the leakage test meaningless.
3. **Odds for the baseline.** CLAUDE.md requires a betting-favorite baseline. mdabbert has per-fight closing odds back to 2010, already joined to bouts. Store them in an `odds` table, attribute CC BY 4.0 in the README, and never use them as model features (keeps the "not a betting tool" framing honest).
4. **Rankings.** jerzyszocik rankings history is CC0, Feb 2013 onward, and updated 2026-09-24, fresher than martj42 (2026-06-06).
5. **Gap fill.** Silver stops at roughly 2026-06-10. The Phase 1b scraper backfills June through today, which is a natural demo of idempotent upserts.

Interview angle: "I seeded from a CC0 snapshot of the same source I scrape, so seed and live data share one schema, and I computed every feature myself so I could prove there was no leakage."

---

## 3. ESPN MMA endpoints

### 3.1 What exists

ESPN has no official public API for this. Community docs describe undocumented JSON endpoints ([pseudo-r/Public-ESPN-API: mma.md](https://github.com/pseudo-r/Public-ESPN-API/blob/main/docs/sports/mma.md); [akeaswaran gist](https://gist.github.com/akeaswaran/b48b02f1c94f873c6655e7129910fc3b)):

**Site API** (`https://site.api.espn.com/apis/site/v2/sports/mma/{league}/`):

- `scoreboard`, `scoreboard?dates=YYYYMMDD`: current/upcoming cards
- `summary?event={id}`: full card with results
- `news`, `athletes/{id}/news`

**Core API** (`https://sports.core.api.espn.com/v2/sports/mma/leagues/{league}/`):

- `events`, `events/{id}`
- `.../competitions/{comp}/competitors/{id}/statistics`: strikes, takedowns, control
- `.../competitors/{id}/linescores`: round-by-round judges' scores
- `.../competitions/{comp}/odds`: pre-fight moneyline
- `.../officials`, `.../status` (method, winner, round)
- `athletes`, `athletes/{id}/eventlog`, `rankings`, `seasons/{year}/rankings`

Same docs: "common/v3 athlete stats are NOT available for MMA" and `athletes?active=true` returns 400 ([mma.md](https://github.com/pseudo-r/Public-ESPN-API/blob/main/docs/sports/mma.md)).

A live call to the UFC scoreboard on 2026-10-01 returned `leagues`, `season`, `events`, `provider` (DraftKings) and competitions with weight class, venue, status, broadcasts, round format, and each competitor's name, flag, and record. No odds block was present for that event ([site.api.espn.com scoreboard](https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard)).

### 3.2 Risks

1. **Terms of use forbid it.** ESPN is covered by the Disney Terms of Use ("may be branded Disney, ABC, ESPN, ..."). They prohibit accessing or extracting content "using a robot, spider, script, or other automated means, including ... for the purposes of creating or developing any AI Tool, data mining or web scraping or otherwise compiling ... any collection of data, data set or database." The license excludes use for "training, testing, benchmarking or validation of any artificial intelligence or machine learning tool," and limits use to "personal, noncommercial use" ([Disney Terms of Use](https://disneytermsofuse.com/english/)). CageOps does all of those things.
2. **No contract, no stability.** "These APIs are not officially supported and may change without notice"; "no official limits published, but excessive requests may be blocked" ([pseudo-r/Public-ESPN-API](https://github.com/pseudo-r/Public-ESPN-API)).
3. **Contract claims are the real exposure.** In *hiQ v. LinkedIn*, scraping public pages wasn't a CFAA violation, but the court enforced the site's user agreement, and the case settled with hiQ ceasing scraping, deleting data, and paying $500,000 ([ZwillGen](https://www.zwillgen.com/alternative-data/hiq-v-linkedin-wrapped-up-web-scraping-lessons-learned/); [Privacy World](https://www.privacyworld.blog/2022/11/federal-court-rules-in-favor-of-linkedins-breach-of-contract-claim-after-six-years-of-cfaa-data-scraping-litigation/)).
4. **Redundant.** Everything ESPN offers (results, stats, odds, rankings) is covered by ufcstats plus the Kaggle sets above.

**Recommendation: drop ESPN from the architecture.** Update the CLAUDE.md diagram and log it as a DECISIONS.md entry. It's a strong interview point: "I found the ToS explicitly prohibits ML use, so I cut the source."

---

## 4. MMA news RSS feeds (Phase 4 RAG)

### 4.1 Feeds checked on 2026-10-01

| Feed | URL | Observed cadence | Content in feed | Status |
|---|---|---|---|---|
| Sherdog news | `https://www.sherdog.com/rss/news.xml` | 50 items spanning 2026-09-28 to 2026-10-01 (~15/day); `ttl` 60 min | Summary only | Verified |
| MMA News | `https://www.mmanews.com/feed` | 50 items spanning 2026-09-28 to 2026-10-01 (~15/day); WebSub enabled | Truncated summary | Verified |
| Cageside Press | `https://cagesidepress.com/feed/` | 11 items spanning 2026-09-29 to 2026-10-01 (~7/day) | **Full text** in `content:encoded` | Verified |
| Sherdog (others) | `/rss/articles.xml`, `/rss/interviews.xml`, `/rss/events.xml`, etc. | Not measured | Not checked | Listed on [Sherdog syndication](https://www.sherdog.com/syndication.php) |
| Bloody Elbow | `https://bloodyelbow.com/rss/current` | n/a | n/a | Listed by [Feedspot](https://rss.feedspot.com/mma_rss_feeds/); `bloodyelbow.com/feed` returned HTTP 402 |
| BJPenn.com | `https://www.bjpenn.com/feed/` | n/a | n/a | Listed by [Feedspot](https://rss.feedspot.com/mma_rss_feeds/); my fetcher was **disallowed by robots.txt** |
| MMA Mania | `https://www.mmamania.com/rss/current.xml` | n/a | n/a | Fetch **disallowed by robots.txt** |
| MMA Fighting, MMA Junkie | (not confirmed) | n/a | n/a | Blocked from my research tools; **[UNVERIFIED]** |

Sources for verified rows: the live feeds themselves ([Sherdog](https://www.sherdog.com/rss/news.xml), [MMA News](https://www.mmanews.com/feed), [Cageside Press](https://cagesidepress.com/feed/)). Cadence is a 3-day sample, not a guarantee. MMA Fighting is owned by Vox Media (SB Nation) ([Wikipedia](https://en.wikipedia.org/wiki/MMA_Fighting)); MMA Junkie by Gannett ([Wikipedia](https://en.wikipedia.org/wiki/MMA_Junkie)).

The robots.txt blocks I hit apply to my research fetcher's user agent. They show that some MMA sites actively block automated and AI agents. Your ingestion worker should check robots.txt per feed with **its own** User-Agent and skip disallowed ones.

### 4.2 Terms that affect retrieval

- **Sherdog** (terms last updated 2021-01-10): no automated system that sends more requests "than a human can reasonably produce," and "you agree not to aggregate or collate any of the content available through the Service for use elsewhere"; also "You will not copy, distribute, or disclose any part of the Service in any medium." No RSS-specific or AI clause ([Sherdog Terms of Use](https://www.sherdog.com/terms-of-use)).
- **MMA News** (terms last updated 2024-09-09): "You agree not to use or launch any automated system, including ... 'robots,' 'spiders,' 'offline readers,' etc., that accesses the Site" and "You agree not to use, modify, copy, or transfer such Company Content in any way or form." No RSS clause ([MMA News Terms](https://www.mmanews.com/terms-of-use/)).
- **Cageside Press**: no terms page found in search ([search results](https://cagesidepress.com/about/)). **[UNVERIFIED]**. Full text in the feed is not a license to republish.
- **ESPN MMA RSS / news endpoint**: covered by the Disney terms in section 3.2. Do not use.

### 4.3 How to use feeds without stepping on those terms

The read-by-feed-reader use RSS exists for is widely tolerated, while the aggregation and copy clauses above target republishing. A defensible design:

1. **Feed only.** Poll RSS no more often than the feed's `ttl` (Sherdog: 60 min). Don't crawl article pages for full text unless the feed provides it.
2. **Store for retrieval, show only snippets.** Keep text internally for embeddings. The UI shows at most a short excerpt (~1-2 sentences), the headline, outlet name, and a link. The roadmap already says this.
3. **Honor removals.** Add a `source_allowlist` config and a purge-by-domain command so one outlet can be removed fast if asked.
4. **Prefer Sherdog + MMA News summaries** for a low-risk start, add Cageside Press, and record each outlet's terms check date in DECISIONS.md.
5. Strictly, Sherdog's "aggregate or collate" clause and MMA News's "not to ... copy" clause can be read to cover any storage. If you want zero ambiguity, email the outlets for permission. For a non-commercial portfolio project, snippet + link + fast takedown is the common practical line, but it is a judgment call, not a guarantee.

---

## 5. Legal and ethical risks

_Not legal advice. These are the risks a reviewer or interviewer is likely to raise, with the mitigation CageOps uses._

| Risk | Why it matters | Mitigation |
|---|---|---|
| **Terms-of-service / contract claims** | Courts enforce site terms against scrapers even when CFAA claims fail ([ZwillGen on hiQ](https://www.zwillgen.com/alternative-data/hiq-v-linkedin-wrapped-up-web-scraping-lessons-learned/)) | Drop ESPN; no logins or accounts anywhere; read each source's terms and log the date checked |
| **CFAA / "unauthorized access"** | Scraping publicly accessible pages without auth barriers generally isn't "without authorization" under the Ninth Circuit's *hiQ* reasoning ([ZwillGen](https://www.zwillgen.com/alternative-data/hiq-v-linkedin-wrapped-up-web-scraping-lessons-learned/)) | Public pages only; never bypass blocks, CAPTCHAs, or rate limits |
| **Copyright in stats** | Facts aren't copyrightable; only original selection/arrangement is ([Feist v. Rural, 499 U.S. 340](https://supreme.justia.com/cases/federal/us/499/340/)) | Store facts (numbers, results), not page layouts; don't redistribute raw HTML |
| **Copyright in news text** | Articles are protected expression | Snippets + links only in the UI; no full-text redistribution |
| **Server load** | Small sites can be hurt by aggressive scraping; Sherdog's terms tie automation limits to human-like request rates ([Sherdog Terms](https://www.sherdog.com/terms-of-use)) | Global ~1 req/s limit in Redis, raw-HTML cache, honor `Crawl-delay`, descriptive User-Agent with contact URL |
| **Trademark / affiliation** | "UFC" belongs to a TKO Group Holdings subsidiary ([Wikipedia: TKO Group Holdings](https://en.wikipedia.org/wiki/TKO_Group_Holdings)) | No UFC logos; name is "CageOps"; explicit non-affiliation disclaimer |
| **Dataset licensing** | Kaggle licenses are uploader-declared and may not match the upstream source; mdabbert's Kaggle (CC BY 4.0) and GitHub (Apache-2.0) labels differ | Attribute every dataset; don't commit raw data to the public repo (already gitignored); link to Kaggle instead |
| **Gambling harm** | A win-probability site can look like a tipster service | No odds-derived picks, no "value bet" language, no sportsbook links, show calibration and uncertainty; CLAUDE.md rule 4 |
| **Personal data** | Fighter DOB, nationality, and news about fighters' lives are personal data | Use only what's needed for features (age at fight date); no social media; honor removal requests |
| **Data accuracy** | Wrong predictions about real people | Model card on /model page; show model version and date on every prediction |

### 5.1 README disclaimer (paste this)

```markdown
## Disclaimer

CageOps is a non-commercial, educational portfolio project that demonstrates
data-engineering and ML-infrastructure practices. It is **not affiliated with,
endorsed by, or sponsored by** the UFC, Zuffa, LLC, TKO Group Holdings, ESPN,
or any news outlet or data provider listed below. "UFC" and related marks are
the property of their respective owners and are used here only to describe the
subject of the data.

**Not betting advice.** Predictions are statistical estimates from an
experimental model, may be wrong, and are published with their measured
accuracy and calibration. Do not use them to place wagers. If gambling is
causing you problems, call or text 1-800-MY-RESET (US National Problem
Gambling Helpline, 24/7).

**Data sources and attribution.**
- Fight and fighter statistics: collected from public pages on
  [ufcstats.com](http://ufcstats.com) at a limited request rate, respecting
  robots.txt. Only factual statistics are stored.
- Historical seed data (Kaggle):
  - "UFC Data: Stats & Rankings & Betting Odds" by jerzyszocik (CC0)
  - "UFC rankings history" by jerzyszocik (CC0)
  - "Ultimate UFC Dataset" by Matt Dabbert (mdabbert), licensed
    [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Used for
    historical odds as a baseline only. Changes: normalized into a relational
    schema.
- News context: headlines, short excerpts, and links from public RSS feeds.
  Full articles remain on the publishers' sites; follow the links to read them.
  No article text is redistributed.

Raw source data is not included in this repository.

**Takedown requests.** If you own content used here and want it removed,
open an issue or email <your-contact-email> and it will be removed promptly.
```

Notes on the disclaimer:

- Update the source list to match what you actually ingest. If you add or drop a feed, edit it.
- CC BY 4.0 requires credit, a license link, and an indication of changes ([CC BY 4.0 deed](https://creativecommons.org/licenses/by/4.0/)). The mdabbert bullet covers all three.
- TKO Group Holdings is the UFC's parent company ([Wikipedia](https://en.wikipedia.org/wiki/TKO_Group_Holdings)); Zuffa is kept in the list because it is the historical UFC entity ([Wikipedia](https://en.wikipedia.org/wiki/Zuffa)).
- 1-800-MY-RESET became the National Problem Gambling Helpline number on 2026-01-29 and supports call, text, and chat ([NCPG](https://www.ncpgambling.org/news/1-800-my-reset-announcement/)). Older guides still cite 1-800-GAMBLER.
- Set your scraper's User-Agent to something like `CageOps/0.1 (+https://github.com/<you>/cageops; <contact-email>)` so site operators can reach you.

---

## Silver Parquet findings (verified 2026-10-02)

Profiled `full_data_silver_plus.parquet` (sha256 in `docs/seed_manifest.json`) with DuckDB.

**Shape.** 8,555 rows, one per fight, 367 columns, `fight_url` unique (no duplicate fights). 774 events, 2,686 distinct fighters. `event_date` runs **1994-03-11 to 2026-05-16**. The file's Kaggle timestamp is 2026-06-10, so June onward comes from the Phase 1b scraper.

**Per-round stats exist.** The layout is wide: `f_{1,2}_r{1..5}_<stat>` with 21 stats per fighter per round (`sig_strikes_succ/att`, `total_strikes_*`, `td_1_succ/att`, `knockdowns`, `submission_att`, `reversals`, `ctrl`, head/body/leg and distance/clinch/ground splits). Whole-fight totals are in `f_{1,2}_<stat>`. The loader has to unpivot this wide layout into a per-round table. Coverage is complete from 2000 onward; 1994 to 1999 has gaps (for example 1994: 29 of 31 fights have round-1 stats). Round sums match the fight totals for sig strikes in 8,534 of 8,534 checked fights.

**Data-quality problems to handle in the loader**
- `result` is inconsistent: `'\n\n        \n KO/TKO \n'` (whitespace-padded) next to plain `'KO/TKO'`, and `'Decision'` next to `'Decision - Unanimous'`. Needs normalizing to one method vocabulary.
- 7 fights have a `winner` that matches neither fighter name (draws / no contests).
- **Fighter order is not random.** `winner == f_1_name` in 5,487 fights vs 3,061 for `f_2` (64%). In the two fights spot-checked, `f_1` was the champion / favorite. So f_1 vs f_2 carries information. The loader must not treat it as meaningful, and training must swap order randomly.

**Leakage risks (CLAUDE.md rule 1)**
- `f_{1,2}_fighter_SlpM`, `Str_Acc`, `SApM`, `Str_Def`, `TD_Avg`, `TD_Acc`, `TD_Def`, `Sub_Avg` and the `w`/`l`/`d`/`nc_dq` record columns are populated for all 8,555 fights. They look like **career snapshots from the time of scraping**, so for an old fight they include that fighter's future fights. **Do not load these as fight-time features.** The feature pipeline (1c) must compute everything from per-round facts.
- Silver also contains `f_{1,2}_ranking`, `*_implied_prob`, `*_odds_legacy`, `*_ko_odds`, `*_sub_odds`, `*_bfo_best_decimal` and `odds_source`. Odds are present for 6,660 fights (legacy 5,530, bfo 1,130) and rankings for 2,009. D-006 assumed silver was "raw facts only". These columns are not raw facts, and we don't know when each was captured, so they are not used as features. Whether to use silver's odds or mdabbert's for the baseline is an open question for the Phase 1a plan.

**Domain spot-check (compare with ufcstats.com).**
- UFC 243, Whittaker vs Adesanya (2019-10-05): Adesanya by KO, round 2 at 3:33; R1 sig 17/66 vs 20/44 with a knockdown for Adesanya in R1 and R2.
- UFC 254, Khabib vs Gaethje (2020-10-24): Khabib by triangle choke, round 2 at 1:34; R1 sig 23/60 vs 23/36, Khabib 1 of 2 takedowns.

## Phase 1a load results (verified 2026-10-02)

Loaded by `uv run python -m cageops_worker.seed`. Each row count below corresponds to the
file hashes in `docs/seed_manifest.json` (silver `bc66bfefb71e`, mdabbert `deb1cd9a7014`,
jerzyszocik rankings `c751b95800cc`, martj42 `a31032fc11db`); a different hash means a
different input, not a code change. The loader refuses to run if a file doesn't match.

| Table | Rows | Notes |
|---|---|---|
| fighters / events / fights | 2,686 / 774 / 8,555 | silver is the only source that creates fights |
| fight_round_stats | 40,244 | 21 fights (1994 to 1998 only) have no round data |
| fight_totals | 17,068 | round sig-strike sums match totals in every checked fight |
| odds, mdabbert | 6,907 | 7,160 of 7,177 rows matched (99.76%); 17 reported, not loaded |
| odds, silver | 6,650 | 125 fights have silver odds and no mdabbert odds |
| fighter_aliases | 124 | all reviewed and scoped by source: 96 `mdabbert`, 16 `jerzyszocik`, 12 `martj42` |
| rankings, jerzyszocik | 88,156 | 474 clean dates, 2013-02-04 to 2025-07-27 |
| rankings, martj42 | 99,516 | 530 clean dates, 2013-02-04 to 2026-06-02 |

- **Favorite agreement:** mdabbert and silver pick the same favorite in 98.1% of the 6,406 fights where both have odds (`bfo` 98.6%, `legacy` 98.0%). This shows the sources are consistent, not that silver's odds were known before the fight, so mdabbert stays the primary baseline source.
- **mdabbert vs silver disagreements (logged in the load report):** 6 red-corner differences (Jotko vs Anders, Baeza vs Brown, Holland vs Hernandez, Landwehr vs Elkins on 2020-05-16; Emmers vs Chikadze 2020-03-07; Martin vs Jandiroba 2019-12-07) and 2 winner differences (Martin vs Jandiroba and Davis vs Jones). Both were checked by hand and are **mdabbert errors**: Jandiroba won (silver has it, mdabbert has the corners flipped), and on ufcstats Mike Davis won Davis vs Jones by unanimous decision, referee Keith Peterson (mdabbert names Mason Jones). They are listed under `verified_mdabbert_errors` in the load report, from `verified_disagreements.csv`. ufcstats-derived data (silver) is canonical, and the other 5 red-corner differences are logged but unverified. The 14 mdabbert fights that silver doesn't have (e.g. Hall vs Souza, 2020-05-09, which was cancelled) are reported and skipped, never inserted.
- **Rankings quality:** jerzyszocik's snapshots from 2025-08-03 onward (63 dates) hold two merged lists and are skipped; martj42 has 2 such dates (2025-09-16, 2026-05-19), also skipped. See D-011. Fights before the first snapshot (2013-02-04, 2,160 fights) have no rankings, and ranks are NULL when the latest snapshot is more than 21 days old.
- **Unmatched ranking names:** 3 of 594 jerzyszocik names (0.51%, 8 rows) and 2 of 623 martj42 names (0.32%, 2 rows). The leftovers are Melissa Dixon and DeAnna Bennett (not in silver) and Jaime Alvarez (a candidate that was rejected as a different person).
- **ufcstats.com and automated clients (found 2026-10-02):** a plain HTTP request to a fight page returned a JavaScript "Checking your browser" page instead of the content, and the site doesn't serve HTTPS. We don't try to get around a bot check. This is a risk for the Phase 1b live scraper, so Phase 1b has to start by checking what the site allows (D-004).

---

## ufcstats page structure (verified 2026-10-03)

Read from the hand-saved fixtures in `services/scraper/tests/fixtures/ufcstats/` (minimized, D-017) and checked against the Phase 1a seed. This is what the Phase 1b parsers (D-018) are built on. Details that surprised us are in bold.

| Page | Gives | Quirks |
|---|---|---|
| Events list (`/statistics/events/completed`, `/upcoming`) | one row per event: id (in the link), name, date, location | **the first row of the completed list is the next upcoming event** (a `next.png` icon; flagged by the parser, not treated as completed); 25 rows per page with pagination links; `?page=all` is used by other scrapers but was not verified here |
| Event page (`/event-details/<id>`) | name, date, location, one row per bout: fight id, both fighters, W/L flag (`win`, `draw`, `nc`), weight class, method **abbreviated** (`U-DEC`, `S-DEC`, `M-DEC`, `KO/TKO`, `SUB`), round, time | **the winner is listed first** (Malott above Burns on a card billed "Burns vs. Malott"), so row order carries the outcome; for a draw the order is the card's; an upcoming event has the same rows with everything but the fighters and weight class blank |
| Fight page (`/fight-details/<id>`) | W/L/D/NC badge per fighter, bout title, method in full (`Decision - Unanimous`), round, time, time format, referee, details (judges' scores, strike or submission name), totals, per-round totals, significant-strike breakdown (head/body/leg, distance/clinch/ground), overall and per round | **first fighter = red corner** (matches the seed's red corner in 6 of 6 fixtures); **no date on the page**; **per-round tables put `<thead>` rows ("Round 1") inside `<tbody>`**; the per-round header says "Td %" twice (a typo; read columns by position); `---` appears for a percentage with zero attempts, while `0 of 0` is a real zero; a 1998 fight has no tables, only "Round-by-round stats not currently available."; the bout title carries the title fight ("UFC Heavyweight Title Bout") and a trailing "Bout" the seed's weight-class table doesn't have; the "Details" text can be junk (`to`) |
| Fighter page (`/fighter-details/<id>`) | name, nickname, height `5' 10"`, reach `71"`, stance, DOB `Jul 20, 1986` | **career rates (SLpM, accuracy, ...) and the record are on the same list, "as of today", and are never read** (rule 1); a missing value is `--` (an empty stance is just blank); heights and reach convert to exactly the seed's centimetres (Burns 177.8 / 180.34) |

Differences between these pages and the seed, now verified by the seed-vs-scrape test (`services/worker/tests/ingest/test_seed_match.py`, D-019): for the six fixture fights, their events, fighters and 32 stat rows, every column matches except these six values (the page wins in each case):
- Vologdin vs Castaneda: seed `outcome = 'unknown'` (silver's winner matched neither fighter), page says draw (majority decision).
- Santos vs Marscucci (1998): seed `finish_time_sec = 27`, page says 10:27 (627 s, "1 Rnd + OT (12-3)"). Probably a seed parse of "0:27".
- Marwan Rahiki: the seed has no height, reach, DOB or stance; the site now publishes 5' 8", 72", May 10, 2002 and Orthodox.

Upcoming bouts (an event page with no results yet) carry the fighters and weight class only: no title flag, round format, corner or result. They are stored as `scheduled` bouts with those fields NULL (D-023).

## Open items before Phase 1

- [x] Check `http://ufcstats.com/robots.txt` and the site footer from your own machine; record results in DECISIONS.md. (D-004)
- [x] Open the jerzyszocik silver Parquet and confirm per-round columns and date range. (see "Silver Parquet findings" below)
- [x] Decide on ESPN (recommended: remove) and update the CLAUDE.md architecture line. (D-005)
- [ ] Confirm MMA Fighting / MMA Junkie feed URLs and terms manually, or leave them out.
- [ ] Phase 1b: find a live rankings source for upcoming fights (martj42 ends 2026-06-02).
- [x] Settle how to reach ufcstats given the browser check above: we don't bypass it. Build against fixtures and a replay server, ask the operator for access, and add a permitted results source in Phase 1d (D-013, 2026-10-03).
