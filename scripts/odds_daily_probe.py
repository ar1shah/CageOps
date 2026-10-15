"""How much of OUR data would the jerzyszocik "UFC Betting Odds (Daily)" dataset cover? (D-034)

    uv run python scripts/odds_daily_probe.py \\
        data/raw/jerzyszocik__ufc-betting-odds-daily-dataset/UFC_betting_odds.csv \\
        --expect-sha256 44fc6838e7ae9698883ac36f5238667a16576067d7b31c2f23077b3066f9f716 [--json]

READ-ONLY. It reads the CSV and the database and writes neither: the connection runs
`SET TRANSACTION READ ONLY`, so a write would fail. It loads nothing (the daily odds loader is
deferred, D-034). The dataset changes upstream every day, so `--expect-sha256` pins the snapshot:
a different file exits 1 before any analysis, and every number in docs/BENCHMARKS.md comes from
the snapshot whose hash is printed with it.

Matching is the rule the existing loaders use: the ufcstats id when the feed row has one, else the
normalized, unordered pair of fighter names on the same date +-1 day (`normalize_name`).

Exit codes: 0 ok, 1 file missing, wrong hash or configuration missing, 2 bad usage.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import hashlib
import json
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, text

from cageops_common.config import get_settings
from cageops_common.db.session import make_engine
from cageops_trainer.splits import default_boundaries
from cageops_trainer.train import WIKIPEDIA_PERIOD_START
from cageops_worker.seed.normalize import normalize_name, ufcstats_id

EXIT_OK, EXIT_ERROR, EXIT_USAGE = 0, 1, 2
SIMILARITY = 0.75  # difflib ratio at or above which two names count as "the same person, spelled
# differently"; also true if the last name token or the sorted tokens agree
DAY_WINDOW = (0, -1, 1)  # tried in this order


@dataclass(frozen=True)
class FeedRow:
    """One snapshot row: one bookmaker's price for one bout at one time."""

    fight_id: str | None  # ufcstats fight id; None when the row has no fight_url (all of 2026)
    name_1: str
    name_2: str
    norm_1: str
    norm_2: str
    odds_1: float | None
    odds_2: float | None
    event_date: date
    added: date | None
    source: str
    region: str

    @property
    def usable(self) -> bool:
        """Both moneyline prices present and valid (decimal odds are > 1)."""
        return bool(self.odds_1 and self.odds_2 and self.odds_1 > 1 and self.odds_2 > 1)


@dataclass(frozen=True)
class OurFight:
    fight_id: int
    ufcstats_id: str | None
    event_date: date
    name_a: str
    name_b: str
    norm_a: str
    norm_b: str
    fighter_a_id: int
    winner_id: int | None
    mdabbert_a: float | None
    mdabbert_b: float | None

    @property
    def has_mdabbert(self) -> bool:
        return self.mdabbert_a is not None

    @property
    def pair(self) -> frozenset[str]:
        return frozenset((self.norm_a, self.norm_b))


# -- reading ---------------------------------------------------------------------------------


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _float(value: str) -> float | None:
    return float(value) if value else None


def parse_feed(path: Path) -> list[FeedRow]:
    rows: list[FeedRow] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for raw in csv.DictReader(handle):
            added = raw["adding_date"]
            rows.append(
                FeedRow(
                    fight_id=ufcstats_id(raw["fight_url"]) if raw["fight_url"] else None,
                    name_1=raw["fighter_1"],
                    name_2=raw["fighter_2"],
                    norm_1=normalize_name(raw["fighter_1"]),
                    norm_2=normalize_name(raw["fighter_2"]),
                    odds_1=_float(raw["odds_1"]),
                    odds_2=_float(raw["odds_2"]),
                    event_date=date.fromisoformat(raw["event_date"]),
                    added=datetime.fromisoformat(added).date() if added else None,
                    source=raw["source"],
                    region=raw["region"],
                )
            )
    return rows


def load_our_fights(conn: Connection) -> list[OurFight]:
    """Fights with a decided winner, with their mdabbert odds if any. Read-only."""
    conn.execute(text("SET TRANSACTION READ ONLY"))
    result = conn.execute(
        text(
            "SELECT f.id, f.ufcstats_id, e.event_date, fa.name AS a, fb.name AS b,"
            " f.fighter_a_id, f.winner_id, o.decimal_odds_a, o.decimal_odds_b"
            " FROM fights f JOIN events e ON e.id = f.event_id"
            " JOIN fighters fa ON fa.id = f.fighter_a_id JOIN fighters fb ON fb.id = f.fighter_b_id"
            " LEFT JOIN odds o ON o.fight_id = f.id AND o.source = 'mdabbert'"
            " WHERE f.status = 'completed' AND f.outcome = 'win'"
            " ORDER BY e.event_date, f.id"
        )
    ).mappings()
    return [
        OurFight(
            fight_id=r["id"],
            ufcstats_id=r["ufcstats_id"],
            event_date=r["event_date"],
            name_a=r["a"],
            name_b=r["b"],
            norm_a=normalize_name(r["a"]),
            norm_b=normalize_name(r["b"]),
            fighter_a_id=r["fighter_a_id"],
            winner_id=r["winner_id"],
            mdabbert_a=float(r["decimal_odds_a"]) if r["decimal_odds_a"] is not None else None,
            mdabbert_b=float(r["decimal_odds_b"]) if r["decimal_odds_b"] is not None else None,
        )
        for r in result
    ]


# -- matching --------------------------------------------------------------------------------


@dataclass
class Feed:
    rows: list[FeedRow]
    by_id: dict[str, list[FeedRow]] = field(default_factory=dict)
    by_day_pair: dict[tuple[date, frozenset[str]], list[FeedRow]] = field(default_factory=dict)
    by_day: dict[date, list[FeedRow]] = field(default_factory=dict)

    @classmethod
    def build(cls, rows: Iterable[FeedRow]) -> Feed:
        feed = cls(rows=list(rows))
        for r in feed.rows:
            if r.fight_id:  # a blank fight_url must never act as an id
                feed.by_id.setdefault(r.fight_id, []).append(r)
            feed.by_day_pair.setdefault((r.event_date, frozenset((r.norm_1, r.norm_2))), []).append(
                r
            )
            feed.by_day.setdefault(r.event_date, []).append(r)
        return feed

    def on_days(self, day: date) -> list[FeedRow]:
        return [r for d in DAY_WINDOW for r in self.by_day.get(day + timedelta(days=d), [])]


def match_fight(our: OurFight, feed: Feed) -> tuple[str | None, list[FeedRow]]:
    """('id' | 'name+date' | None, usable feed rows)."""
    if our.ufcstats_id and our.ufcstats_id in feed.by_id:
        usable = [r for r in feed.by_id[our.ufcstats_id] if r.usable]
        if usable:
            return "id", usable
    for offset in DAY_WINDOW:
        key = (our.event_date + timedelta(days=offset), our.pair)
        usable = [r for r in feed.by_day_pair.get(key, []) if r.usable]
        if usable:
            return "name+date", usable
    return None, []


def _similar(a: str, b: str) -> bool:
    """Plausibly the same person spelled differently. Deliberately loose: it is only used to
    sort unmatched fights into 'spelling variant' or 'different opponent' for a human to review."""
    if sorted(a.split()) == sorted(b.split()):
        return True
    if a.split()[-1:] == b.split()[-1:]:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= SIMILARITY


def classify_unmatched(our: OurFight, feed: Feed) -> str:
    """Why a fight has no exact match: 'bout not in feed', 'spelling variant' (one fighter matches
    exactly and the other is a similar name on the same date), or 'different opponent' (one matches
    and the other is not similar: a replacement, or a spelling difference this rule can't see)."""
    day_rows = feed.on_days(our.event_date)
    pairings = [
        (r.norm_1, r.norm_2) for r in day_rows if {r.norm_1, r.norm_2} & {our.norm_a, our.norm_b}
    ]
    if not pairings:
        return "bout not in feed"
    if any(frozenset(p) == our.pair for p in pairings):
        return "in feed, no usable price"
    for first, second in pairings:
        for ours, theirs in ((our.norm_a, our.norm_b), (our.norm_b, our.norm_a)):
            if ours in (first, second):
                other = second if ours == first else first
                if _similar(theirs, other):
                    return "spelling variant"
    return "different opponent"


# -- the consensus price ---------------------------------------------------------------------


@dataclass(frozen=True)
class Consensus:
    p_a: float  # P(our fighter A wins): de-vigged, oriented per row, averaged over bookmakers
    bookmakers: int
    days_before: int  # the freshest snapshot, in days before the event


def a_is_first(our: OurFight, row: FeedRow) -> bool:
    """Is OUR fighter A this row's fighter_1? Bookmakers do not all list the same fighter first,
    so every row is oriented on its own. Exact normalized name first; if the row matched by ufcstats
    id and the spelling differs, whichever of its two names is closer to ours."""
    if our.norm_a == row.norm_1:
        return True
    if our.norm_a == row.norm_2:
        return False
    close_1 = difflib.SequenceMatcher(None, our.norm_a, row.norm_1).ratio()
    close_2 = difflib.SequenceMatcher(None, our.norm_a, row.norm_2).ratio()
    return close_1 >= close_2


def consensus(our: OurFight, rows: Sequence[FeedRow]) -> Consensus | None:
    """The last snapshot per (bookmaker, region) on or before the event day, de-vigged to
    P(A wins), then averaged. Snapshots added after the event are ignored."""
    last: dict[tuple[str, str], FeedRow] = {}
    for r in rows:
        if not r.usable or r.added is None or r.added > our.event_date:
            continue
        key = (r.source, r.region)
        if key not in last or r.added > (last[key].added or date.min):
            last[key] = r
    if not last:
        return None
    probabilities = []
    for r in last.values():
        implied_1, implied_2 = 1.0 / r.odds_1, 1.0 / r.odds_2  # type: ignore[operator]
        p_first = implied_1 / (implied_1 + implied_2)
        probabilities.append(p_first if a_is_first(our, r) else 1.0 - p_first)
    freshest = max(r.added for r in last.values() if r.added)
    return Consensus(statistics.mean(probabilities), len(last), (our.event_date - freshest).days)


# -- the report ------------------------------------------------------------------------------


def _share(n: int, d: int) -> float | None:
    return round(n / d, 4) if d else None


def profile(rows: Sequence[FeedRow]) -> dict[str, Any]:
    by_year: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        if r.added:
            year = str(r.event_date.year) if r.event_date.year >= 2025 else "2024 and earlier"
            by_year[year].append((r.added - r.event_date).days)
    return {
        "rows": len(rows),
        "distinct_fight_ids": len({r.fight_id for r in rows if r.fight_id}),
        "rows_blank_fight_url": sum(1 for r in rows if not r.fight_id),
        "rows_blank_adding_date": sum(1 for r in rows if r.added is None),
        "rows_with_both_moneyline_prices": sum(1 for r in rows if r.usable),
        "event_date_first": min(r.event_date for r in rows).isoformat(),
        "event_date_last": max(r.event_date for r in rows).isoformat(),
        "adding_date_first": min(r.added for r in rows if r.added).isoformat(),
        "adding_date_last": max(r.added for r in rows if r.added).isoformat(),
        "sources": dict(Counter(r.source for r in rows).most_common()),
        "added_minus_event_days_by_event_year": {
            year: {
                "rows": len(lags),
                "median": statistics.median(lags),
                "added_on_or_before_event_day": _share(sum(1 for d in lags if d <= 0), len(lags)),
            }
            for year, lags in sorted(by_year.items())
        },
    }


def coverage(results: Sequence[tuple[OurFight, str | None, list[FeedRow]]]) -> dict[str, Any]:
    n = len(results)
    matched = [x for x in results if x[1]]
    added = [x for x in matched if not x[0].has_mdabbert]
    return {
        "fights": n,
        "mdabbert": sum(1 for x in results if x[0].has_mdabbert),
        "daily_matched": len(matched),
        "matched_by_id": sum(1 for x in matched if x[1] == "id"),
        "matched_by_name_and_date": sum(1 for x in matched if x[1] == "name+date"),
        "adds_beyond_mdabbert": len(added),
        "union_with_mdabbert": sum(1 for x in results if x[0].has_mdabbert or x[1]),
    }


def analyse(feed_rows: Sequence[FeedRow], ours: Sequence[OurFight]) -> dict[str, Any]:
    feed = Feed.build(feed_rows)
    results = []
    for fight in ours:
        how, rows = match_fight(fight, feed)
        results.append((fight, how, rows))

    last_event = max(f.event_date for f in ours)
    bounds = default_boundaries(last_event)
    wiki = WIKIPEDIA_PERIOD_START

    def window(lo: date | None, hi: date | None):
        return [
            x
            for x in results
            if (lo is None or x[0].event_date >= lo) and (hi is None or x[0].event_date < hi)
        ]

    test = window(bounds.test_start, None)
    report: dict[str, Any] = {
        "windows": {
            "val_start": bounds.val_start.isoformat(),
            "test_start": bounds.test_start.isoformat(),
            "wikipedia_period_start": wiki.isoformat(),
        },
        "our_database": {
            "labeled_fights": len(ours),
            "labeled_fights_with_mdabbert_odds": sum(1 for f in ours if f.has_mdabbert),
            "last_labeled_event": last_event.isoformat(),
        },
        "coverage": {
            "test": coverage(test),
            "test_before_wikipedia_period": coverage([x for x in test if x[0].event_date < wiki]),
            "test_from_wikipedia_period": coverage([x for x in test if x[0].event_date >= wiki]),
            "validation": coverage(window(bounds.val_start, bounds.test_start)),
            "train": coverage(window(None, bounds.val_start)),
            "by_year": {
                str(y): coverage([x for x in results if x[0].event_date.year == y])
                for y in sorted({x[0].event_date.year for x in results})
            },
        },
    }

    # Test fights with no price in EITHER source: why?
    neither = [x[0] for x in test if not x[0].has_mdabbert and not x[1]]

    def breakdown(fights: Sequence[OurFight]) -> dict[str, int]:
        return dict(Counter(classify_unmatched(f, feed) for f in fights))

    report["test_fights_with_no_odds_in_either_source"] = {
        "total": len(neither),
        "before_wikipedia_period": breakdown([f for f in neither if f.event_date < wiki]),
        "from_wikipedia_period": breakdown([f for f in neither if f.event_date >= wiki]),
        "all": breakdown(neither),
        "similarity_rule": (
            "same person if the sorted name tokens or the last token agree, "
            f"or the difflib ratio is >= {SIMILARITY}"
        ),
    }
    variants = report["test_fights_with_no_odds_in_either_source"]["all"].get("spelling variant", 0)
    other = report["test_fights_with_no_odds_in_either_source"]["all"].get("different opponent", 0)
    union = report["coverage"]["test"]["union_with_mdabbert"]
    report["test_ceiling_with_alias_review"] = {
        "exact_union": union,
        "lower_bound_if_variants_resolved": union + variants,
        "upper_bound_if_every_one_name_match_resolved": union + variants + other,
        "of": len(test),
    }

    # Is the consensus price any good? Favorite accuracy on test, and agreement with mdabbert.
    correct = scored = new_correct = new_scored = agree = overlap = 0
    books: list[int] = []
    lags: list[int] = []
    for fight, how, rows in test:
        if not how:
            continue
        c = consensus(fight, rows)
        if not c:
            continue
        books.append(c.bookmakers)
        lags.append(c.days_before)
        p_a = c.p_a
        if p_a == 0.5:
            continue
        won_a = fight.winner_id == fight.fighter_a_id
        ok = (p_a > 0.5) == won_a
        scored += 1
        correct += ok
        if not fight.has_mdabbert:
            new_scored += 1
            new_correct += ok
        elif fight.mdabbert_a != fight.mdabbert_b:
            overlap += 1
            agree += (p_a > 0.5) == (fight.mdabbert_a < fight.mdabbert_b)  # type: ignore[operator]
    report["test_consensus_sanity"] = {
        "fights_with_a_pre_fight_consensus": len(books),
        "favorite_correct": correct,
        "favorite_scored": scored,
        "favorite_correct_on_fights_mdabbert_lacks": new_correct,
        "favorite_scored_on_fights_mdabbert_lacks": new_scored,
        "agrees_with_mdabbert_favorite": agree,
        "overlapping_fights_compared": overlap,
        "bookmakers_per_fight_median": statistics.median(books) if books else None,
        "bookmakers_per_fight_min": min(books) if books else None,
        "bookmakers_per_fight_max": max(books) if books else None,
        "days_last_snapshot_before_event_median": statistics.median(lags) if lags else None,
        "share_last_snapshot_within_1_day": _share(sum(1 for d in lags if d <= 1), len(lags)),
    }
    return report


def render(report: dict[str, Any]) -> str:
    snap, prof, db = report["snapshot"], report["profile"], report["our_database"]
    lines = [
        f"snapshot  {snap['path']}",
        f"sha256    {snap['sha256']}",
        f"size      {snap['size_bytes']:,} bytes",
        "",
        f"feed: {prof['rows']:,} rows, {prof['distinct_fight_ids']:,} distinct ufcstats fight ids; "
        f"{prof['rows_blank_fight_url']:,} rows have no fight_url, "
        f"{prof['rows_blank_adding_date']:,} no adding_date",
        f"      event dates {prof['event_date_first']} .. {prof['event_date_last']}; "
        f"added {prof['adding_date_first']} .. {prof['adding_date_last']}",
        f"our database: {db['labeled_fights']:,} labeled fights, "
        f"{db['labeled_fights_with_mdabbert_odds']:,} with mdabbert odds",
        "",
    ]
    cov = report["coverage"]
    lines.append(
        f"{'':34}{'fights':>7}{'mdabbert':>9}{'daily':>7}{'by id':>7}"
        f"{'by name':>8}{'adds':>6}{'union':>7}"
    )
    for label, key in (
        ("test", "test"),
        ("  before 2026-05-17", "test_before_wikipedia_period"),
        ("  from 2026-05-17 (Wikipedia)", "test_from_wikipedia_period"),
        ("validation", "validation"),
        ("train", "train"),
    ):
        c = cov[key]
        lines.append(
            f"{label:34}{c['fights']:>7}{c['mdabbert']:>9}{c['daily_matched']:>7}"
            f"{c['matched_by_id']:>7}{c['matched_by_name_and_date']:>8}"
            f"{c['adds_beyond_mdabbert']:>6}{c['union_with_mdabbert']:>7}"
        )
    n = report["test_fights_with_no_odds_in_either_source"]
    ceil = report["test_ceiling_with_alias_review"]
    lines += [
        "",
        f"test fights with no odds in either source: {n['total']}  {n['all']}",
        f"  before 2026-05-17: {n['before_wikipedia_period']}",
        f"  from 2026-05-17:   {n['from_wikipedia_period']}",
        f"  rule: {n['similarity_rule']}",
        f"test coverage ceiling: exact union {ceil['exact_union']} of {ceil['of']}; "
        f"{ceil['lower_bound_if_variants_resolved']} if the spelling variants are resolved; "
        f"at most {ceil['upper_bound_if_every_one_name_match_resolved']}",
    ]
    s = report["test_consensus_sanity"]
    lines += [
        "",
        f"consensus favorite correct: {s['favorite_correct']}/{s['favorite_scored']}; "
        f"on fights mdabbert lacks: {s['favorite_correct_on_fights_mdabbert_lacks']}/"
        f"{s['favorite_scored_on_fights_mdabbert_lacks']}; agrees with mdabbert on "
        f"{s['agrees_with_mdabbert_favorite']}/{s['overlapping_fights_compared']}",
        f"bookmakers per fight median {s['bookmakers_per_fight_median']}; last snapshot median "
        f"{s['days_last_snapshot_before_event_median']} days before the event, "
        f"{s['share_last_snapshot_within_1_day']} within 1 day",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None, *, conn: Connection | None = None, out=None) -> int:
    parser = argparse.ArgumentParser(
        prog="odds_daily_probe.py", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument("csv", type=Path)
    parser.add_argument("--expect-sha256", help="refuse (exit 1) if the file's sha256 differs")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    args = parser.parse_args(argv)
    out = out or sys.stdout

    if not args.csv.is_file():
        print(f"ERROR: {args.csv} does not exist", file=sys.stderr)
        return EXIT_ERROR
    digest = sha256_of(args.csv)
    if args.expect_sha256 and digest != args.expect_sha256:
        print(
            f"ERROR: {args.csv} has sha256 {digest} but --expect-sha256 says {args.expect_sha256}. "
            "The dataset changes upstream daily; use the pinned snapshot.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    rows = parse_feed(args.csv)
    if conn is None:
        try:
            engine = make_engine(get_settings().database_url)
        except Exception as exc:  # missing DATABASE_URL
            print(
                f"ERROR: configuration is missing or invalid ({exc}); see .env.example",
                file=sys.stderr,
            )
            return EXIT_ERROR
        with engine.connect() as connection:
            ours = load_our_fights(connection)
    else:
        ours = load_our_fights(conn)

    report = analyse(rows, ours)
    report["snapshot"] = {
        "path": str(args.csv),
        "sha256": digest,
        "size_bytes": args.csv.stat().st_size,
    }
    report["profile"] = profile(rows)
    print(json.dumps(report, indent=2, default=str) if args.json else render(report), file=out)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
