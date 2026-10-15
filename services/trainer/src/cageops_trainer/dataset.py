"""Reading the labeled fights, their features and the baselines' inputs, in one snapshot, after
checking the features are not stale (D-033).

Two things refuse training, and only two:
1. `fight_features` is not one complete build (two rows for every labeled fight).
2. The input fingerprint the build stored no longer matches the database.
Timestamps (a newer seed load, a newer fetch) only warn: once scraping is scheduled there will
nearly always be a fetch newer than the last build, and one that changed no input leaves the
fingerprint equal.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import numpy as np
from sqlalchemy import Connection, text

from cageops_common.db.fingerprint import features_input_fingerprint
from cageops_common.inference.features import NUMERIC_FEATURES
from cageops_common.inference.rows import number, rows_to_arrays

REBUILD = "run: uv run python -m cageops_worker.features rebuild"


class StaleFeatures(Exception):
    """Training was refused because fight_features cannot be trusted. The message says why."""


@dataclass(frozen=True)
class FeatureBuild:
    load_run_id: int
    output_sha256: str
    inputs_sha256: str
    started_at: datetime
    warnings: list[str] = field(default_factory=list)


def check_features(conn: Connection) -> FeatureBuild:
    run_ids = list(conn.execute(text("SELECT DISTINCT load_run_id FROM fight_features")).scalars())
    if not run_ids:
        raise StaleFeatures(f"fight_features is empty; {REBUILD}")
    if len(run_ids) > 1:
        raise StaleFeatures(
            f"fight_features holds rows from {len(run_ids)} different builds "
            f"(load_run_id {sorted(run_ids)}); {REBUILD}"
        )
    run = (
        conn.execute(
            text("SELECT id, sha256, started_at, report FROM load_runs WHERE id = :i"),
            {"i": run_ids[0]},
        )
        .mappings()
        .one()
    )

    missing = list(
        conn.execute(
            text(
                "SELECT f.id FROM completed_fights f"
                " LEFT JOIN (SELECT fight_id, count(*) AS n FROM fight_features"
                "            GROUP BY fight_id) c ON c.fight_id = f.id"
                " WHERE f.outcome = 'win' AND COALESCE(c.n, 0) <> 2 ORDER BY f.id"
            )
        ).scalars()
    )
    if missing:
        sample = ", ".join(str(i) for i in missing[:5])
        raise StaleFeatures(
            f"{len(missing)} labeled fights do not have exactly two feature rows "
            f"(fight ids {sample}{', ...' if len(missing) > 5 else ''}); {REBUILD}"
        )

    warnings = _timestamp_warnings(conn, run["started_at"])
    stored = (run["report"] or {}).get("inputs_sha256")
    if stored is None:
        raise StaleFeatures(
            f"the features build (load_run_id {run['id']}) was made before the input fingerprint "
            f"existed, so it cannot be checked; {REBUILD}"
        )
    current = features_input_fingerprint(conn)
    if current != stored:
        hint = (
            f" Newer loads were seen ({'; '.join(warnings)}), which is the likely cause."
            if warnings
            else ""
        )
        raise StaleFeatures(
            "the fights, bios or stats the features were built from have changed since the "
            f"build (load_run_id {run['id']}, inputs {stored[:12]}... now {current[:12]}...)."
            f"{hint} {REBUILD}"
        )
    return FeatureBuild(
        load_run_id=run["id"],
        output_sha256=run["sha256"],
        inputs_sha256=stored,
        started_at=run["started_at"],
        warnings=warnings,
    )


def _timestamp_warnings(conn: Connection, since: datetime) -> list[str]:
    """Loads and fetches that happened after the build started. Information, not a refusal."""
    warnings = []
    loads = conn.execute(
        text(
            "SELECT source, count(*) AS runs, max(finished_at) AS newest,"
            " count(*) FILTER (WHERE finished_at IS NULL) AS unfinished"
            " FROM load_runs WHERE source <> 'features'"
            " AND (finished_at IS NULL OR finished_at > :t) GROUP BY source ORDER BY source"
        ),
        {"t": since},
    ).mappings()
    for row in loads:
        detail = f"{row['runs']} load run(s) of {row['source']} after the features build"
        if row["unfinished"]:
            detail += f" ({row['unfinished']} unfinished)"
        warnings.append(detail)
    pages = (
        conn.execute(
            text(
                "SELECT count(*) AS n, max(fetched_at) AS newest FROM raw_pages"
                " WHERE fetched_at > :t"
            ),
            {"t": since},
        )
        .mappings()
        .one()
    )
    if pages["n"]:
        warnings.append(
            f"{pages['n']} raw page(s) fetched after the features build "
            f"(newest {pages['newest'].isoformat()})"
        )
    return warnings


@dataclass
class Dataset:
    """Every fight with a decided winner, in (event date, fight id) order, as arrays.

    `a` is fighter_a (the lower id, D-009), `b` the other. Nothing here has been swapped."""

    fight_id: np.ndarray
    event_date: list[date]
    fighter_a: np.ndarray
    fighter_b: np.ndarray
    winner_is_a: np.ndarray  # bool
    method: np.ndarray  # str: decision | ko_tko | submission | dq
    a_values: np.ndarray  # (n, len(NUMERIC_FEATURES)), NaN for NULL
    b_values: np.ndarray
    a_stance: list[str]
    b_stance: list[str]
    weight_class: list[str | None]
    schedule: np.ndarray  # (n, 2): is_title_fight, scheduled_rounds; NaN for NULL
    odds_a: np.ndarray  # mdabbert decimal odds, NaN where none
    odds_b: np.ndarray
    rank_a: np.ndarray  # division rank as of the fight (0 = champion), NaN where not ranked
    rank_b: np.ndarray

    def __len__(self) -> int:
        return len(self.fight_id)

    @property
    def dates(self) -> np.ndarray:
        return np.array(self.event_date, dtype="datetime64[D]")

    def odds_fingerprint(self) -> dict[str, Any]:
        """sha256 and count of the (fight, odds) rows the favorite baseline reads, so a metric
        can be tied to the odds snapshot it came from."""
        have = ~np.isnan(self.odds_a) & ~np.isnan(self.odds_b)
        rows = [
            [int(f), float(a), float(b)]
            for f, a, b in zip(
                self.fight_id[have], self.odds_a[have], self.odds_b[have], strict=True
            )
        ]
        digest = hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()
        return {"rows": len(rows), "sha256": digest}


def load_dataset(conn: Connection) -> Dataset:
    fights = (
        conn.execute(
            text(
                "SELECT f.id AS fight_id, e.event_date, f.fighter_a_id, f.fighter_b_id,"
                " f.winner_id, f.method FROM completed_fights f"
                " JOIN events e ON e.id = f.event_id WHERE f.outcome = 'win'"
                " ORDER BY e.event_date, f.id"
            )
        )
        .mappings()
        .all()
    )
    columns = ", ".join(NUMERIC_FEATURES)
    features = {
        (r["fight_id"], r["fighter_id"]): r
        for r in conn.execute(
            text(
                f"SELECT fight_id, fighter_id, {columns}, stance, weight_class,"
                " is_title_fight, scheduled_rounds FROM fight_features"
            )
        ).mappings()
    }
    odds = {
        r["fight_id"]: (float(r["decimal_odds_a"]), float(r["decimal_odds_b"]))
        for r in conn.execute(
            text(
                "SELECT fight_id, decimal_odds_a, decimal_odds_b FROM odds"
                " WHERE source = 'mdabbert'"
                " AND decimal_odds_a IS NOT NULL AND decimal_odds_b IS NOT NULL"
            )
        ).mappings()
    }
    ranks = {
        (r["fight_id"], r["fighter_id"]): float(r["rank"])
        for r in conn.execute(
            text(
                "SELECT fight_id, fighter_id, rank FROM fight_pre_rankings"
                " WHERE ranking_type = 'division' AND status = 'ranked'"
            )
        ).mappings()
    }

    n = len(fights)
    schedule = np.full((n, 2), np.nan)
    # One function turns a feature row into the model's input, here and in the API.
    rows_a = [features[(f["fight_id"], f["fighter_a_id"])] for f in fights]
    rows_b = [features[(f["fight_id"], f["fighter_b_id"])] for f in fights]
    a_values, a_stance = rows_to_arrays(rows_a)
    b_values, b_stance = rows_to_arrays(rows_b)
    weight_class = [a["weight_class"] for a in rows_a]
    for i, a in enumerate(rows_a):
        schedule[i] = [
            number(None if a["is_title_fight"] is None else int(a["is_title_fight"])),
            number(a["scheduled_rounds"]),
        ]

    fight_ids = np.array([f["fight_id"] for f in fights], dtype=np.int64)
    return Dataset(
        fight_id=fight_ids,
        event_date=[f["event_date"] for f in fights],
        fighter_a=np.array([f["fighter_a_id"] for f in fights], dtype=np.int64),
        fighter_b=np.array([f["fighter_b_id"] for f in fights], dtype=np.int64),
        winner_is_a=np.array([f["winner_id"] == f["fighter_a_id"] for f in fights], dtype=bool),
        method=np.array([f["method"] for f in fights]),
        a_values=a_values,
        b_values=b_values,
        a_stance=a_stance,
        b_stance=b_stance,
        weight_class=weight_class,
        schedule=schedule,
        odds_a=np.array([odds.get(f["fight_id"], (np.nan, np.nan))[0] for f in fights]),
        odds_b=np.array([odds.get(f["fight_id"], (np.nan, np.nan))[1] for f in fights]),
        rank_a=np.array([ranks.get((f["fight_id"], f["fighter_a_id"]), np.nan) for f in fights]),
        rank_b=np.array([ranks.get((f["fight_id"], f["fighter_b_id"]), np.nan) for f in fights]),
    )
