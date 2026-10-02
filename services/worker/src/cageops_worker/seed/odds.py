"""Load moneyline odds from two sources into one `odds` table with source tracking.

- mdabbert (primary baseline source): matched to fights by fighter names and date.
- silver (gap filler): matched by ufcstats ids; its odds columns carry no capture date.

Odds are never model features (baseline only). `captured_at` stays NULL for both because
neither source says when the odds were taken.
"""

from __future__ import annotations

import csv
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
from sqlalchemy import Engine, func, text
from sqlalchemy.dialects.postgresql import insert

from cageops_common.db.models import LoadRun, Odds
from cageops_worker.seed.normalize import american_to_decimal, ufcstats_id
from cageops_worker.seed.resolver import NameResolver, load_aliases
from cageops_worker.seed.weight_class import fight_weight_class

# More red-corner disagreements than this between silver and mdabbert means the two files
# disagree about who was in the red corner, so the loader stops instead of guessing.
MAX_RED_DISAGREEMENTS = 10
SAMPLE = 25


class RedCornerDisagreementError(RuntimeError):
    def __init__(self, report: dict[str, Any]):
        super().__init__(
            f"{report['red_corner_disagree']} fights where silver and mdabbert name different "
            f"red-corner fighters (limit {MAX_RED_DISAGREEMENTS}); nothing was written"
        )
        self.report = report


@dataclass
class FightRef:
    fight_id: int
    a: int
    b: int
    event_date: date
    red: int | None
    winner: int | None
    outcome: str


@dataclass
class MdabbertRow:
    red_name: str
    blue_name: str
    date: date
    red_odds: float | None
    blue_odds: float | None
    winner: str  # Red | Blue | Draw | No Contest
    weight_class: str | None


@dataclass
class MatchResult:
    odds: list[dict[str, Any]] = field(default_factory=list)
    outcome_updates: list[dict[str, Any]] = field(default_factory=list)
    report: dict[str, Any] = field(default_factory=dict)


def _float(value: str) -> float | None:
    try:
        number = float(value)
    except ValueError:
        return None
    return None if math.isnan(number) else number


def read_mdabbert(path: Path) -> list[MdabbertRow]:
    with path.open(newline="") as f:
        return [
            MdabbertRow(
                red_name=r["R_fighter"],
                blue_name=r["B_fighter"],
                date=datetime.strptime(r["date"], "%Y-%m-%d").date(),
                red_odds=_float(r["R_odds"]),
                blue_odds=_float(r["B_odds"]),
                winner=r["Winner"],
                weight_class=fight_weight_class(r["weight_class"]).canonical,
            )
            for r in csv.DictReader(f)
        ]


def match_mdabbert(
    rows: list[MdabbertRow], fights: list[FightRef], resolver: NameResolver
) -> MatchResult:
    """Match each mdabbert row to a fight: exact date first, then +/-1 day. Pure function."""
    by_pair: dict[frozenset[int], list[FightRef]] = defaultdict(list)
    for fight in fights:
        by_pair[frozenset((fight.a, fight.b))].append(fight)

    counts: Counter[str] = Counter()
    unmatched_names: list[tuple[str, str, str]] = []
    unmatched_fights: list[tuple[str, str, str]] = []
    red_disagreements: list[tuple[str, str, str]] = []
    winner_disagreements: list[tuple[str, str, str]] = []
    result = MatchResult()
    seen: dict[int, MdabbertRow] = {}

    for row in rows:
        red = resolver.resolve(row.red_name, weight_class=row.weight_class, on=row.date)
        blue = resolver.resolve(row.blue_name, weight_class=row.weight_class, on=row.date)
        if red.fighter_id is None or blue.fighter_id is None:
            counts["unmatched_name"] += 1
            for name, res in ((row.red_name, red), (row.blue_name, blue)):
                if res.fighter_id is None:
                    unmatched_names.append((str(row.date), name, res.status))
            continue
        candidates = by_pair.get(frozenset((red.fighter_id, blue.fighter_id)), [])
        exact = [f for f in candidates if f.event_date == row.date]
        near = [f for f in candidates if abs(f.event_date - row.date) <= timedelta(days=1)]
        if len(exact) == 1:
            fight, how = exact[0], "matched_exact_date"
        elif not exact and len(near) == 1:
            fight, how = near[0], "matched_plus_minus_1_day"
        elif len(exact) > 1 or len(near) > 1:
            counts["ambiguous_fight_match"] += 1
            continue
        else:
            counts["unmatched_no_fight"] += 1
            unmatched_fights.append((str(row.date), row.red_name, row.blue_name))
            continue
        if fight.fight_id in seen:
            counts["duplicate_fight_match"] += 1
            continue
        seen[fight.fight_id] = row
        counts[how] += 1

        # Corner check: both sources must agree on who was red.
        if fight.red is not None:
            if fight.red == red.fighter_id:
                counts["red_corner_agree"] += 1
            else:
                counts["red_corner_disagree"] += 1
                red_disagreements.append((str(row.date), row.red_name, row.blue_name))
        # Winner check: a different winner means we matched the wrong fight.
        if row.winner in ("Red", "Blue"):
            expected = red.fighter_id if row.winner == "Red" else blue.fighter_id
            if fight.winner == expected:
                counts["winner_agree"] += 1
            else:
                counts["winner_disagree"] += 1
                winner_disagreements.append((str(row.date), row.red_name, row.blue_name))
        elif fight.outcome == "unknown":
            outcome = "draw" if row.winner == "Draw" else "no_contest"
            result.outcome_updates.append({"id": fight.fight_id, "outcome": outcome})
            counts[f"outcome_filled_{outcome}"] += 1

        if row.red_odds is not None and row.blue_odds is not None:
            odds_by_fighter = {
                red.fighter_id: american_to_decimal(row.red_odds),
                blue.fighter_id: american_to_decimal(row.blue_odds),
            }
            result.odds.append(
                {
                    "fight_id": fight.fight_id,
                    "source": "mdabbert",
                    "source_detail": None,
                    "decimal_odds_a": odds_by_fighter[fight.a],
                    "decimal_odds_b": odds_by_fighter[fight.b],
                    "captured_at": None,
                }
            )
        else:
            counts["matched_without_odds"] += 1

    result.report = {
        "source": "mdabbert",
        "rows_read": len(rows),
        **counts,
        "odds_rows": len(result.odds),
        "unmatched_name_samples": unmatched_names[:SAMPLE],
        "unmatched_fight_samples": unmatched_fights[:SAMPLE],
        "red_corner_disagreements": red_disagreements[:SAMPLE],
        "winner_disagreements": winner_disagreements[:SAMPLE],
    }
    return result


def _fight_refs(conn) -> list[FightRef]:
    return [
        FightRef(*row)
        for row in conn.execute(
            text(
                "SELECT f.id, f.fighter_a_id, f.fighter_b_id, e.event_date, f.red_fighter_id,"
                " f.winner_id, f.outcome FROM fights f JOIN events e ON e.id = f.event_id"
            )
        )
    ]


def _upsert_odds(conn, rows: list[dict[str, Any]]) -> None:
    for start in range(0, len(rows), 2000):
        stmt = insert(Odds)
        stmt = stmt.on_conflict_do_update(
            index_elements=["fight_id", "source"],
            set_={
                c: stmt.excluded[c]
                for c in ("source_detail", "decimal_odds_a", "decimal_odds_b", "captured_at")
            },
        )
        conn.execute(stmt, rows[start : start + 2000])


def load_mdabbert(engine: Engine, path: Path, sha256: str) -> dict[str, Any]:
    rows = read_mdabbert(path)
    with engine.begin() as conn:
        load_aliases(conn)
        resolver = NameResolver.from_db(conn)
        result = match_mdabbert(rows, _fight_refs(conn), resolver)
        if result.report.get("red_corner_disagree", 0) > MAX_RED_DISAGREEMENTS:
            raise RedCornerDisagreementError(result.report)
        run_id = conn.execute(
            insert(LoadRun)
            .values(source="mdabbert", file_path=str(path), sha256=sha256, rows_read=len(rows))
            .returning(LoadRun.id)
        ).scalar_one()
        _upsert_odds(conn, result.odds)
        if result.outcome_updates:
            conn.execute(
                text("UPDATE fights SET outcome = :outcome WHERE id = :id AND outcome = 'unknown'"),
                result.outcome_updates,
            )
        matched = result.report.get("matched_exact_date", 0) + result.report.get(
            "matched_plus_minus_1_day", 0
        )
        result.report["rows_rejected"] = len(rows) - matched
        conn.execute(
            LoadRun.__table__.update()
            .where(LoadRun.id == run_id)
            .values(
                finished_at=func.now(),
                rows_loaded=len(result.odds),
                rows_rejected=len(rows) - matched,
                report=result.report,
            )
        )
    return result.report


def silver_odds_rows(
    parquet_rows: list[dict[str, Any]],
    fighter_ids: dict[str, int],
    fight_refs: dict[str, tuple[int, int, int]],
) -> tuple[list[dict[str, Any]], Counter[str]]:
    """Odds from silver's own columns. `legacy` rows use f_n_odds_legacy, `bfo` rows use
    f_n_bfo_best_decimal. Both are decimal odds."""
    counts: Counter[str] = Counter()
    out = []
    for row in parquet_rows:
        detail = row.get("odds_source")
        if detail not in ("legacy", "bfo"):
            counts["no_odds_source"] += 1
            continue
        column = "odds_legacy" if detail == "legacy" else "bfo_best_decimal"
        d1, d2 = row.get(f"f_1_{column}"), row.get(f"f_2_{column}")
        if d1 is None or d2 is None or d1 <= 1 or d2 <= 1:
            counts["unusable_decimal_odds"] += 1
            continue
        fight_id, a, b = fight_refs[ufcstats_id(row["fight_url"])]
        by_fighter = {
            fighter_ids[ufcstats_id(row["f_1_url"])]: d1,
            fighter_ids[ufcstats_id(row["f_2_url"])]: d2,
        }
        out.append(
            {
                "fight_id": fight_id,
                "source": "silver",
                "source_detail": detail,
                "decimal_odds_a": Decimal(str(round(by_fighter[a], 3))),
                "decimal_odds_b": Decimal(str(round(by_fighter[b], 3))),
                "captured_at": None,
            }
        )
        counts[f"silver_{detail}"] += 1
    return out, counts


def load_silver_odds(engine: Engine, path: Path, sha256: str) -> dict[str, Any]:
    parquet_rows = pq.read_table(path).to_pylist()
    with engine.begin() as conn:
        fighter_ids = dict(conn.execute(text("SELECT ufcstats_id, id FROM fighters")).all())
        fights = {
            r[0]: (r[1], r[2], r[3])
            for r in conn.execute(
                text("SELECT ufcstats_id, id, fighter_a_id, fighter_b_id FROM fights")
            )
        }
        odds, counts = silver_odds_rows(parquet_rows, fighter_ids, fights)
        run_id = conn.execute(
            insert(LoadRun)
            .values(
                source="silver_odds",
                file_path=str(path),
                sha256=sha256,
                rows_read=len(parquet_rows),
            )
            .returning(LoadRun.id)
        ).scalar_one()
        _upsert_odds(conn, odds)
        report = {
            "source": "silver_odds",
            "rows_read": len(parquet_rows),
            **counts,
            "odds_rows": len(odds),
            "rows_rejected": len(parquet_rows) - len(odds),
        }
        conn.execute(
            LoadRun.__table__.update()
            .where(LoadRun.id == run_id)
            .values(
                finished_at=func.now(),
                rows_loaded=len(odds),
                rows_rejected=report["rows_rejected"],
                report=report,
            )
        )
    return report


AGREEMENT_SQL = text(
    """
    WITH pair AS (
        SELECT m.fight_id, s.source_detail,
               (m.decimal_odds_a < m.decimal_odds_b) AS m_a_favorite,
               (s.decimal_odds_a < s.decimal_odds_b) AS s_a_favorite
        FROM odds m JOIN odds s ON s.fight_id = m.fight_id AND s.source = 'silver'
        WHERE m.source = 'mdabbert'
          AND m.decimal_odds_a <> m.decimal_odds_b AND s.decimal_odds_a <> s.decimal_odds_b
    )
    SELECT coalesce(source_detail, 'all') AS source_detail, count(*) AS fights,
           count(*) FILTER (WHERE m_a_favorite = s_a_favorite) AS same_favorite
    FROM pair GROUP BY ROLLUP(source_detail) ORDER BY 1
    """
)


def odds_summary(engine: Engine) -> dict[str, Any]:
    """Coverage and the favorite-agreement number between the two sources, from the DB."""
    with engine.connect() as conn:
        coverage = conn.execute(
            text(
                "SELECT (SELECT count(*) FROM fights),"
                " (SELECT count(*) FROM odds WHERE source = 'mdabbert'),"
                " (SELECT count(*) FROM odds WHERE source = 'silver'),"
                " (SELECT count(DISTINCT fight_id) FROM odds),"
                " (SELECT count(*) FROM odds s WHERE s.source = 'silver' AND NOT EXISTS"
                "   (SELECT 1 FROM odds m WHERE m.fight_id = s.fight_id AND m.source = 'mdabbert'))"
            )
        ).one()
        agreement = {
            row.source_detail: {
                "fights": row.fights,
                "same_favorite": row.same_favorite,
                "pct": round(100 * row.same_favorite / row.fights, 1),
            }
            for row in conn.execute(AGREEMENT_SQL)
        }
    return {
        "fights": coverage[0],
        "fights_with_mdabbert_odds": coverage[1],
        "fights_with_silver_odds": coverage[2],
        "fights_with_any_odds": coverage[3],
        "silver_only_odds_fights": coverage[4],
        "favorite_agreement": agreement,
    }
