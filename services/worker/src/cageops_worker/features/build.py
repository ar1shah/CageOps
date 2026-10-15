"""Rebuild `fight_features` and `fighter_features_current` from scratch in one transaction.

A failure anywhere rolls the whole thing back, so the old tables survive. Like the seed loaders,
a failed run leaves no `load_runs` row (the row is written inside the same transaction); the
only trace is the exit code and stderr (see D-027).
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import date
from typing import Any

from sqlalchemy import Engine, func, insert, text

from cageops_common.db.fingerprint import features_input_fingerprint
from cageops_common.db.models import FighterFeaturesCurrent, FightFeatures, LoadRun
from cageops_common.inference.asof import earliest_upcoming_date
from cageops_worker.features.compute import (
    DURATION_NULL_REASONS,
    Subject,
    compute_features,
)
from cageops_worker.features.history import (
    FightRecord,
    History,
    has_unresolved_prior_fight,
    load_history,
    prior_fights,
)

BATCH = 5000
BOOKKEEPING_COLUMNS = ("built_at", "load_run_id")
# Every column that describes a fight and a fighter. Tests compare these, never the bookkeeping.
FEATURE_COLUMNS = [
    c.name for c in FightFeatures.__table__.columns if c.name not in BOOKKEEPING_COLUMNS
]
# The same for `fighter_features_current` (D-037): one row per fighter, as of the build date.
CURRENT_COLUMNS = [
    c.name for c in FighterFeaturesCurrent.__table__.columns if c.name not in BOOKKEEPING_COLUMNS
]
# Not computed from `compute_features`' output by name: they describe the row, not the history.
_CURRENT_OWN = ("fighter_id", "built_as_of", "last_fight_date", "dob")


class FeatureBuildError(Exception):
    """The build's own checks failed. `report` says what; nothing was written."""

    def __init__(self, message: str, report: dict[str, Any]):
        super().__init__(message)
        self.report = report


def target_fights(history: History, today: date) -> tuple[list[FightRecord], list[FightRecord]]:
    """(fights to build rows for, past-dated scheduled fights left out).

    Targets are every completed fight plus scheduled fights that haven't happened yet. A bout
    dated before `today` that never got a result is neither history nor a real upcoming fight,
    so it is excluded and reported (D-027), with one day of grace: a bout dated yesterday and still
    `scheduled` is on a late card (it ran past midnight New York time and its results have not
    been ingested), so it is still built (D-037).
    """
    earliest = earliest_upcoming_date(today)
    upcoming = [f for f in history.scheduled if f.event_date >= earliest]
    stale = [f for f in history.scheduled if f.event_date < earliest]
    targets = sorted(history.completed + upcoming, key=lambda f: f.fight_id)
    return targets, sorted(stale, key=lambda f: f.fight_id)


def build_rows(history: History, targets: list[FightRecord], today: date) -> list[dict[str, Any]]:
    rows = []
    for fight in targets:
        for fighter_id in fight.fighter_ids:
            bio = history.bios[fighter_id]
            subject = Subject(
                event_date=fight.event_date,
                dob=bio.dob,
                height_cm=bio.height_cm,
                reach_cm=bio.reach_cm,
                stance=bio.stance,
                weight_class=fight.weight_class,
                is_title_fight=fight.is_title_fight,
                scheduled_rounds=fight.scheduled_rounds,
            )
            features = compute_features(
                prior_fights(history, fighter_id, fight.event_date),
                subject,
                has_unresolved_prior_fight=has_unresolved_prior_fight(
                    history, fighter_id, fight.event_date, today
                ),
            )
            rows.append({"fight_id": fight.fight_id, "fighter_id": fighter_id, **features})
    return rows


def build_current_rows(history: History, today: date) -> list[dict[str, Any]]:
    """One row per fighter: their history as of `today` (completed fights strictly before it).

    Same `prior_fights` and `compute_features` as `build_rows`, so the two tables cannot disagree
    about what a fighter had done. `age_days` and `days_since_last_fight` are left out on purpose;
    `last_fight_date` and `dob` are what they are derived from (D-037).
    """
    rows = []
    for fighter_id in sorted(history.bios):
        bio = history.bios[fighter_id]
        prior = prior_fights(history, fighter_id, today)
        features = compute_features(
            prior,
            Subject(
                event_date=today,
                dob=bio.dob,
                height_cm=bio.height_cm,
                reach_cm=bio.reach_cm,
                stance=bio.stance,
                weight_class=None,
                is_title_fight=None,
                scheduled_rounds=None,
            ),
            has_unresolved_prior_fight=has_unresolved_prior_fight(
                history, fighter_id, today, today
            ),
        )
        row = {name: features[name] for name in CURRENT_COLUMNS if name not in _CURRENT_OWN}
        rows.append(
            {
                "fighter_id": fighter_id,
                "built_as_of": today,
                "last_fight_date": prior[-1].event_date if prior else None,
                "dob": bio.dob,
                **row,
            }
        )
    return rows


def fingerprint(rows: list[dict[str, Any]]) -> str:
    """A hash of the output rows (no timestamps or run ids), so two builds from the same data
    can be compared with one string."""
    payload = json.dumps(rows, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def build_report(
    history: History,
    targets: list[FightRecord],
    stale: list[FightRecord],
    rows: list[dict[str, Any]],
    today: date,
) -> dict[str, Any]:
    per_fight = Counter(r["fight_id"] for r in rows)
    reasons = Counter(f.duration_null_reason for f in history.completed if f.duration_null_reason)
    through = max((f.event_date for f in history.completed), default=None)
    return {  # JSON-safe: it is stored in load_runs.report (JSONB)
        "source": "features",
        "today": today.isoformat(),
        "history_through": through.isoformat() if through else None,
        "fights_completed": len(history.completed),
        "fights_upcoming": len(targets) - len(history.completed),
        "rows": len(rows),
        "rows_with_unresolved_prior_fight": sum(r["has_unresolved_prior_fight"] for r in rows),
        "excluded_past_dated_scheduled": {
            "count": len(stale),
            "fights": [
                {"fight_id": f.fight_id, "event_date": f.event_date.isoformat()} for f in stale
            ],
        },
        "completed_fights_with_null_duration": {
            "total": sum(reasons.values()),
            **{reason: reasons.get(reason, 0) for reason in DURATION_NULL_REASONS},
        },
        "fights_without_exactly_two_rows": [
            f.fight_id for f in targets if per_fight.get(f.fight_id, 0) != 2
        ],
    }


def rebuild(engine: Engine, today: date) -> dict[str, Any]:
    with engine.begin() as conn:
        history = load_history(conn)
        # What the build read, as one hash. The trainer recomputes it and refuses to train on
        # features whose inputs have changed since (D-033). Read in this transaction, right
        # after the load, so it describes the same data.
        inputs_sha256 = features_input_fingerprint(conn)
        targets, stale = target_fights(history, today)
        rows = build_rows(history, targets, today)
        current_rows = build_current_rows(history, today)
        report = build_report(history, targets, stale, rows, today)
        if report["fights_without_exactly_two_rows"]:
            raise FeatureBuildError("some fights did not produce exactly 2 rows", report)

        report["inputs_sha256"] = inputs_sha256
        report["output_sha256"] = fingerprint(rows)  # fight_features only, as since D-027
        report["fighters_current"] = len(current_rows)
        report["current_as_of"] = today.isoformat()
        report["current_output_sha256"] = fingerprint(current_rows)
        run_id = conn.execute(
            insert(LoadRun)
            .values(
                source="features",
                file_path="db:completed_fights",
                sha256=report["output_sha256"],
                rows_read=len(targets),
            )
            .returning(LoadRun.id)
        ).scalar_one()
        # A full refresh: every row is derived, so a changed rule or an overturned result can't
        # leave stale rows behind. Readers see the old table until this transaction commits.
        conn.execute(text("DELETE FROM fight_features"))
        for start in range(0, len(rows), BATCH):
            batch = [r | {"load_run_id": run_id} for r in rows[start : start + BATCH]]
            conn.execute(insert(FightFeatures), batch)
        # Same transaction and the same run id: a reader sees the old pair or the new pair.
        conn.execute(text("DELETE FROM fighter_features_current"))
        for start in range(0, len(current_rows), BATCH):
            batch = [r | {"load_run_id": run_id} for r in current_rows[start : start + BATCH]]
            conn.execute(insert(FighterFeaturesCurrent), batch)
        conn.execute(
            LoadRun.__table__.update()
            .where(LoadRun.id == run_id)
            .values(finished_at=func.now(), rows_loaded=len(rows), rows_rejected=0, report=report)
        )
    return report
