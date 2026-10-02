"""Load weekly UFC rankings from two sources into `rankings`.

Quality rule (D-011): a snapshot date is skipped entirely if any fighter appears twice in one
list on that date. That is the signature of two lists merged under one date, which can't be
separated afterwards. The raw file is untouched on disk; skipped dates are reported.

Fighter names are resolved with the source-scoped resolver. Rows whose name can't be matched
to exactly one fighter are kept with fighter_id NULL and listed in the report.
"""

from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, func, text
from sqlalchemy.dialects.postgresql import insert

from cageops_common.db.models import LoadRun, Ranking
from cageops_worker.seed.normalize import normalize_name
from cageops_worker.seed.resolver import ALIASES_CSV, NameResolver, load_aliases
from cageops_worker.seed.weight_class import lookup

BATCH = 5000
UNMATCHED_LIST_LIMIT = 80


@dataclass(frozen=True)
class RankingRow:
    snapshot_date: date
    ranking_type: str  # division | pound_for_pound
    weight_class: str  # canonical
    name_raw: str
    rank: int


# Rank-0 rows with these names mean "the title is vacant", not a fighter.
VACANT_NAMES = {"", "NA"}


def read_rankings(path: Path) -> tuple[list[RankingRow], int]:
    """Returns (rows, number of vacant-slot placeholder rows that were skipped)."""
    rows = []
    vacant = 0
    with path.open(newline="") as f:
        for r in csv.DictReader(f):
            if r["fighter"].strip() in VACANT_NAMES:
                vacant += 1
                continue
            weight_class = lookup(r["weightclass"])  # unknown spellings fail the load
            rows.append(
                RankingRow(
                    snapshot_date=datetime.strptime(r["date"], "%Y-%m-%d").date(),
                    ranking_type="pound_for_pound" if weight_class.pound_for_pound else "division",
                    weight_class=weight_class.canonical,
                    name_raw=r["fighter"].strip(),
                    rank=int(r["rank"]),
                )
            )
    return rows, vacant


def clean_snapshots(rows: list[RankingRow]) -> tuple[list[RankingRow], list[date]]:
    """Drop every snapshot date on which a fighter appears twice in the same list."""
    seen: Counter[tuple[date, str, str, str]] = Counter(
        (r.snapshot_date, r.ranking_type, r.weight_class, normalize_name(r.name_raw)) for r in rows
    )
    bad_dates = {key[0] for key, count in seen.items() if count > 1}
    return [r for r in rows if r.snapshot_date not in bad_dates], sorted(bad_dates)


def build_report(
    source: str,
    rows: list[RankingRow],
    kept: list[RankingRow],
    dropped_dates: list[date],
    resolutions: dict[tuple[str, str], Any],
    vacant_rows: int = 0,
) -> dict[str, Any]:
    all_dates = {r.snapshot_date for r in rows}
    kept_dates = {r.snapshot_date for r in kept}
    names = {normalize_name(r.name_raw): r.name_raw for r in kept}
    status_by_name = {n: resolutions[(source, n)] for n in names}
    unmatched = {n: s for n, s in status_by_name.items() if s.fighter_id is None}
    row_counts = Counter(normalize_name(r.name_raw) for r in kept)
    return {
        "source": f"rankings:{source}",
        "rows_read": len(rows),
        "rows_loaded": len(kept),
        "rows_rejected": len(rows) - len(kept),
        "vacant_title_rows_skipped": vacant_rows,
        "snapshot_dates_total": len(all_dates),
        "snapshot_dates_loaded": len(kept_dates),
        "snapshot_dates_skipped_merged_lists": len(dropped_dates),
        "skipped_dates_first": str(dropped_dates[0]) if dropped_dates else None,
        "skipped_dates_last": str(dropped_dates[-1]) if dropped_dates else None,
        "first_loaded_date": str(min(kept_dates)) if kept_dates else None,
        "last_loaded_date": str(max(kept_dates)) if kept_dates else None,
        "distinct_names": len(names),
        "names_unmatched": len(unmatched),
        "names_unmatched_pct": round(100 * len(unmatched) / max(1, len(names)), 2),
        "rows_with_null_fighter": sum(row_counts[n] for n in unmatched),
        "unmatched_names": sorted(
            (
                {"name": names[n], "status": s.status, "rows": row_counts[n]}
                for n, s in unmatched.items()
            ),
            key=lambda d: -d["rows"],
        )[:UNMATCHED_LIST_LIMIT],
    }


def load_rankings(
    engine: Engine,
    source: str,
    path: Path,
    sha256: str,
    aliases_csv: Path | None = ALIASES_CSV,
) -> dict[str, Any]:
    rows, vacant_rows = read_rankings(path)
    kept, dropped_dates = clean_snapshots(rows)
    with engine.begin() as conn:
        if aliases_csv is not None:
            load_aliases(conn, aliases_csv)
        resolver = NameResolver.from_db(conn)

        # Resolve each (name, list, date) once; the same name repeats across ~500 snapshots.
        cache: dict[tuple[str, str, str | None, date], Any] = {}
        resolutions: dict[tuple[str, str], Any] = {}
        db_rows = []
        for r in kept:
            division = r.weight_class if r.ranking_type == "division" else None
            # Narrow shared names by division and date; a first resolution is reused only
            # when the name is unambiguous, otherwise each snapshot is resolved on its own.
            key = (normalize_name(r.name_raw), r.ranking_type, division, r.snapshot_date)
            if key not in cache:
                cache[key] = resolver.resolve(
                    r.name_raw, source=source, weight_class=division, on=r.snapshot_date
                )
            result = cache[key]
            previous = resolutions.get((source, key[0]))
            if previous is None or (previous.fighter_id is None and result.fighter_id is not None):
                resolutions[(source, key[0])] = result
            db_rows.append(
                {
                    "source": source,
                    "snapshot_date": r.snapshot_date,
                    "ranking_type": r.ranking_type,
                    "weight_class": r.weight_class,
                    "name_raw": r.name_raw,
                    "fighter_id": result.fighter_id,
                    "rank": r.rank,
                }
            )

        report = build_report(source, rows, kept, dropped_dates, resolutions, vacant_rows)
        run_id = conn.execute(
            insert(LoadRun)
            .values(
                source=report["source"], file_path=str(path), sha256=sha256, rows_read=len(rows)
            )
            .returning(LoadRun.id)
        ).scalar_one()
        # A full refresh of this source: its rows are entirely derived from the file, so a
        # changed quality rule can't leave stale rows behind.
        conn.execute(text("DELETE FROM rankings WHERE source = :s"), {"s": source})
        for start in range(0, len(db_rows), BATCH):
            conn.execute(insert(Ranking), db_rows[start : start + BATCH])
        conn.execute(
            LoadRun.__table__.update()
            .where(LoadRun.id == run_id)
            .values(
                finished_at=func.now(),
                rows_loaded=len(db_rows),
                rows_rejected=report["rows_rejected"],
                report=report,
            )
        )
    return report
