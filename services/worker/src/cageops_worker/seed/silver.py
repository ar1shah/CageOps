"""Parse and load the jerzyszocik silver file: fighters, events, fights, per-round stats.

`parse_silver_row` is pure (dict in, plain records out). `load_silver` does the database
work. Scrape-time career stats, silver's rankings and silver's odds columns are NOT read here
(D-008); odds are loaded separately with source tracking.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
from sqlalchemy import Engine, Table, func, select
from sqlalchemy.dialects.postgresql import insert

from cageops_common.db.models import Event, Fight, Fighter, FightRoundStats, FightTotals, LoadRun
from cageops_worker.seed.normalize import (
    clean_text,
    normalize_result,
    parse_finish_time,
    ufcstats_id,
)
from cageops_worker.seed.weight_class import fight_weight_class

MAX_ROUNDS = 5
BATCH = 2000

# model column -> silver column suffix, for stats reported per round
ROUND_COLUMNS = {
    "knockdowns": "knockdowns",
    "sig_strikes_landed": "sig_strikes_succ",
    "sig_strikes_att": "sig_strikes_att",
    "total_strikes_landed": "total_strikes_succ",
    "total_strikes_att": "total_strikes_att",
    "takedowns_landed": "td_1_succ",
    "takedowns_att": "td_1_att",
    "submission_att": "submission_att",
    "reversals": "reversals",
    "ctrl_sec": "ctrl",
    "head_landed": "head_succ",
    "head_att": "head_att",
    "body_landed": "body_succ",
    "body_att": "body_att",
    "leg_landed": "leg_succ",
    "leg_att": "leg_att",
    "distance_landed": "distance_succ",
    "distance_att": "distance_att",
    "clinch_landed": "clinch_succ",
    "clinch_att": "clinch_att",
    "ground_landed": "ground_succ",
    "ground_att": "ground_att",
}
# model column -> silver column suffix, for whole-fight totals
TOTAL_COLUMNS = {
    "knockdowns": "knockdowns",
    "sig_strikes_landed": "sig_strikes_succ",
    "sig_strikes_att": "sig_strikes_att",
    "total_strikes_landed": "total_strikes_succ",
    "total_strikes_att": "total_strikes_att",
    "takedowns_landed": "takedown_succ",
    "takedowns_att": "takedown_att",
    "submission_att": "submission_att",
    "reversals": "reversals",
    "ctrl_sec": "ctrl_time_sec",
}


def _value(row: dict[str, Any], key: str) -> Any:
    """A column value with NaN treated as missing."""
    value = row.get(key)
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


@dataclass
class ParsedFight:
    fight: dict[str, Any]
    fighters: list[dict[str, Any]]
    event: dict[str, Any]
    # keyed by (slot, round) so the loader can map slots to fighter ids later
    round_stats: list[dict[str, Any]] = field(default_factory=list)
    totals: list[dict[str, Any]] = field(default_factory=list)
    unresolved_winner: bool = False
    title_from_weight_class: bool = False


def parse_silver_row(row: dict[str, Any]) -> ParsedFight:
    slots = (1, 2)
    fighters = []
    for n in slots:
        fighters.append(
            {
                "slot": n,
                "ufcstats_id": ufcstats_id(row[f"f_{n}_url"]),
                "name": row[f"f_{n}_name"].strip(),
                "dob": _value(row, f"f_{n}_fighter_dob"),
                "height_cm": _value(row, f"f_{n}_fighter_height_cm"),
                "reach_cm": _value(row, f"f_{n}_fighter_reach_cm"),
                "stance": clean_text(_value(row, f"f_{n}_fighter_stance")),
            }
        )

    method = normalize_result(_value(row, "result"), _value(row, "result_details"))
    weight_class = fight_weight_class(clean_text(_value(row, "weight_class")))
    winner_name = clean_text(_value(row, "winner"))
    winner_slot = next((f["slot"] for f in fighters if f["name"] == winner_name), None)

    round_stats = []
    for n in slots:
        for rnd in range(1, MAX_ROUNDS + 1):
            values = {col: _value(row, f"f_{n}_r{rnd}_{src}") for col, src in ROUND_COLUMNS.items()}
            if all(v is None for v in values.values()):
                continue  # no data for this round: no row, never zeros
            round_stats.append({"slot": n, "round": rnd, **values})

    totals = []
    for n in slots:
        values = {col: _value(row, f"f_{n}_{src}") for col, src in TOTAL_COLUMNS.items()}
        if any(v is not None for v in values.values()):
            totals.append({"slot": n, **values})

    scheduled = _value(row, "num_rounds")
    return ParsedFight(
        fight={
            "ufcstats_id": ufcstats_id(row["fight_url"]),
            "event_ufcstats_id": ufcstats_id(row["event_url"]),
            "slot_winner": winner_slot,
            "weight_class": weight_class.canonical,
            "gender": row["gender"],
            "is_title_fight": bool(row["title_fight"]) or weight_class.title_fight,
            "scheduled_rounds": scheduled or None,  # 0 means "no limit / unknown" in early cards
            "method": method.method,
            "decision_type": method.decision_type,
            "method_detail": method.method_detail,
            "finish_round": _value(row, "finish_round"),
            "finish_time_sec": parse_finish_time(_value(row, "finish_time")),
            "referee": clean_text(_value(row, "referee")),
            "has_round_stats": bool(round_stats),
        },
        fighters=fighters,
        event={
            "ufcstats_id": ufcstats_id(row["event_url"]),
            "name": row["event_name"].strip(),
            "event_date": row["event_date"],
            "city": clean_text(_value(row, "event_city")),
            "state": clean_text(_value(row, "event_state")),
            "country": clean_text(_value(row, "event_country")),
        },
        round_stats=round_stats,
        totals=totals,
        unresolved_winner=winner_slot is None,
        title_from_weight_class=weight_class.title_fight and not row["title_fight"],
    )


def _upsert(conn, table: Table, rows: Sequence[dict[str, Any]], keys: Sequence[str]) -> None:
    """INSERT ... ON CONFLICT (keys) DO UPDATE, in batches. Makes every load idempotent."""
    columns = {name for row in rows for name in row}
    rows = [{name: row.get(name) for name in columns} for row in rows]  # uniform keys
    for start in range(0, len(rows), BATCH):
        stmt = insert(table)
        update = {
            c.name: stmt.excluded[c.name]
            for c in table.columns
            if c.name not in keys and c.name != "id"
        }
        if update:
            stmt = stmt.on_conflict_do_update(index_elements=list(keys), set_=update)
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=list(keys))
        conn.execute(stmt, list(rows[start : start + BATCH]))


def _id_map(conn, table: Table) -> dict[str, int]:
    stmt = select(table.c.ufcstats_id, table.c.id).where(table.c.ufcstats_id.is_not(None))
    return dict(conn.execute(stmt).all())


def read_silver(path: Path) -> list[dict[str, Any]]:
    rows = pq.read_table(path).to_pylist()
    rows.sort(key=lambda r: (r["event_date"], r["fight_url"]))  # oldest first
    return rows


def _rounds_sum_mismatches(parsed: Iterable[ParsedFight]) -> int:
    """Fights where per-round sig strikes don't add up to the reported fight total."""
    bad = 0
    for p in parsed:
        for total in p.totals:
            rounds = [r for r in p.round_stats if r["slot"] == total["slot"]]
            if not rounds or total["sig_strikes_landed"] is None:
                continue
            if sum(r["sig_strikes_landed"] or 0 for r in rounds) != total["sig_strikes_landed"]:
                bad += 1
    return bad


def load_silver(engine: Engine, path: Path, sha256: str) -> dict[str, Any]:
    rows = read_silver(path)
    parsed = [parse_silver_row(r) for r in rows]

    # Later fights overwrite earlier ones, so each fighter's bio comes from their latest row.
    fighters: dict[str, dict[str, Any]] = {}
    for p in parsed:
        for f in p.fighters:
            merged = fighters.setdefault(f["ufcstats_id"], {})
            merged.update({k: v for k, v in f.items() if k != "slot" and v is not None})
    events = {p.event["ufcstats_id"]: p.event for p in parsed}

    report: dict[str, Any] = {"source": "silver", "rows_read": len(rows)}
    with engine.begin() as conn:
        run_id = conn.execute(
            insert(LoadRun)
            .values(source="silver", file_path=str(path), sha256=sha256, rows_read=len(rows))
            .returning(LoadRun.id)
        ).scalar_one()

        # Insert fighters in ufcstats_id order. Ids then follow a hash-like order unrelated to
        # who won, which keeps `fighter_a_id < fighter_b_id` a neutral ordering (D-009).
        fighter_rows = [fighters[k] for k in sorted(fighters)]
        _upsert(conn, Fighter.__table__, fighter_rows, ["ufcstats_id"])
        _upsert(conn, Event.__table__, list(events.values()), ["ufcstats_id"])
        fighter_ids = _id_map(conn, Fighter.__table__)
        event_ids = _id_map(conn, Event.__table__)

        fight_rows, slot_ids = [], {}
        for p in parsed:
            ids = {f["slot"]: fighter_ids[f["ufcstats_id"]] for f in p.fighters}
            a, b = sorted(ids.values())
            fight = dict(p.fight)
            winner_slot = fight.pop("slot_winner")
            winner_id = ids[winner_slot] if winner_slot else None
            fight_rows.append(
                {
                    **{k: v for k, v in fight.items() if k != "event_ufcstats_id"},
                    "event_id": event_ids[fight["event_ufcstats_id"]],
                    "fighter_a_id": a,
                    "fighter_b_id": b,
                    "red_fighter_id": ids[1],  # silver's first slot is the red corner (D-009)
                    "result_source": "ufcstats",
                    "outcome": "win" if winner_id else "unknown",
                    "winner_id": winner_id,
                }
            )
            slot_ids[fight["ufcstats_id"]] = ids
        _upsert(conn, Fight.__table__, fight_rows, ["ufcstats_id"])
        fight_ids = _id_map(conn, Fight.__table__)

        round_rows, total_rows = [], []
        for p in parsed:
            fid, ids = fight_ids[p.fight["ufcstats_id"]], slot_ids[p.fight["ufcstats_id"]]
            for r in p.round_stats:
                round_rows.append(
                    {"fight_id": fid, "fighter_id": ids[r["slot"]]}
                    | {k: v for k, v in r.items() if k != "slot"}
                )
            for t in p.totals:
                total_rows.append(
                    {"fight_id": fid, "fighter_id": ids[t["slot"]]}
                    | {k: v for k, v in t.items() if k != "slot"}
                )
        _upsert(conn, FightRoundStats.__table__, round_rows, ["fight_id", "fighter_id", "round"])
        _upsert(conn, FightTotals.__table__, total_rows, ["fight_id", "fighter_id"])

        by_year = Counter(
            p.event["event_date"].year for p in parsed if not p.fight["has_round_stats"]
        )
        report |= {
            "fighters": len(fighter_rows),
            "events": len(events),
            "fights": len(fight_rows),
            "fight_round_stats": len(round_rows),
            "fight_totals": len(total_rows),
            "fights_without_round_stats": sum(by_year.values()),
            "fights_without_round_stats_by_year": dict(sorted(by_year.items())),
            "fights_with_unresolved_winner": sum(p.unresolved_winner for p in parsed),
            "title_fights_inferred_from_weight_class": sum(
                p.title_from_weight_class for p in parsed
            ),
            "rounds_not_summing_to_totals": _rounds_sum_mismatches(parsed),
            "rows_rejected": 0,
        }
        conn.execute(
            LoadRun.__table__.update()
            .where(LoadRun.id == run_id)
            .values(
                finished_at=func.now(),
                rows_loaded=len(fight_rows),
                rows_rejected=0,
                report=report,
            )
        )
    return report
