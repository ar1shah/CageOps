"""Who has completed fights with no per-round stats, and why (D-025, D-029, D-039).

A fight "has stats" for the feature builder when BOTH fighters have a `fight_totals` row
(`compute.window_features`: `f.own and f.opp`), so that is the test here too. Read-only.

The cause is read from `fights.result_source` rather than guessed from a date: a Wikipedia result
has no stats until ufcstats access returns, while a stat-less ufcstats fight is an old one the
source never had numbers for (the 1990s).
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import Engine, text

# One row per (fight, fighter) for completed fights, flagged when the fight has no stats.
# `completed_fights` is the one definition of "completed"; since migration 0011 it exposes
# `result_source`, so nothing here repeats its predicate.
_SIDES = """
WITH cf AS (
  SELECT f.id AS fight_id, e.event_date, f.result_source, f.fighter_a_id AS a, f.fighter_b_id AS b,
         (SELECT count(*) FROM fight_totals t WHERE t.fight_id = f.id) >= 2 AS has_stats
  FROM completed_fights f JOIN events e ON e.id = f.event_id
),
sides AS (
  SELECT fight_id, event_date, result_source, has_stats, a AS fighter_id FROM cf
  UNION ALL
  SELECT fight_id, event_date, result_source, has_stats, b FROM cf
)
"""

_SUMMARY = (
    _SIDES
    + """
SELECT
  (SELECT count(*) FROM cf) AS completed_fights,
  (SELECT count(*) FROM cf WHERE NOT has_stats) AS fights_without_stats,
  (SELECT count(*) FROM cf WHERE NOT has_stats AND result_source = 'wikipedia') AS wikipedia,
  (SELECT count(*) FROM cf WHERE NOT has_stats AND result_source = 'ufcstats') AS ufcstats,
  (SELECT max(event_date) FROM cf) AS history_through,
  (SELECT count(DISTINCT fighter_id) FROM sides) AS fighters_with_a_completed_fight
"""
)

# Per fighter: how many of their stat-less fights each source supplied.
_PER_FIGHTER = (
    _SIDES
    + """
SELECT fighter_id,
       count(*) FILTER (WHERE result_source = 'wikipedia') AS wikipedia_fights,
       count(*) FILTER (WHERE result_source = 'ufcstats') AS ufcstats_fights
FROM sides WHERE NOT has_stats
GROUP BY fighter_id
"""
)

# The ROADMAP's own query: fighters with a completed fight on or after the date, stats or not.
_SINCE = (
    _SIDES
    + """
SELECT count(DISTINCT fighter_id) FROM sides WHERE event_date >= :since
"""
)
_SINCE_GAP = (
    _SIDES
    + """
SELECT count(DISTINCT fighter_id) FROM sides WHERE event_date >= :since AND NOT has_stats
"""
)


def stats_gap(engine: Engine, since: date | None = None) -> dict[str, Any]:
    with engine.connect() as conn:
        summary = dict(conn.execute(text(_SUMMARY)).mappings().one())
        fighters = conn.execute(text(_PER_FIGHTER)).mappings().all()
        report: dict[str, Any] = {
            "completed_fights": summary["completed_fights"],
            "history_through": summary["history_through"].isoformat()
            if summary["history_through"]
            else None,
            "fights_without_stats": {
                "total": summary["fights_without_stats"],
                "result_source_wikipedia": summary["wikipedia"],
                "result_source_ufcstats": summary["ufcstats"],
            },
            "fighters_with_a_completed_fight": summary["fighters_with_a_completed_fight"],
            "fighters_with_any_gap": {
                "total": len(fighters),
                # a gap fighter is counted once, under what their stat-less fights are
                "wikipedia_only": sum(
                    1 for r in fighters if r["wikipedia_fights"] and not r["ufcstats_fights"]
                ),
                "ufcstats_only": sum(
                    1 for r in fighters if r["ufcstats_fights"] and not r["wikipedia_fights"]
                ),
                "both": sum(1 for r in fighters if r["wikipedia_fights"] and r["ufcstats_fights"]),
            },
        }
        if since is not None:
            report["since"] = {
                "date": since.isoformat(),
                "fighters_with_a_completed_fight": conn.execute(
                    text(_SINCE), {"since": since}
                ).scalar_one(),
                "fighters_with_a_fight_lacking_stats": conn.execute(
                    text(_SINCE_GAP), {"since": since}
                ).scalar_one(),
            }
    return report


def format_report(report: dict[str, Any]) -> str:
    gap = report["fighters_with_any_gap"]
    lines = [
        f"completed fights: {report['completed_fights']} (through {report['history_through']})",
        "fights without per-round stats: "
        f"{report['fights_without_stats']['total']} "
        f"(Wikipedia {report['fights_without_stats']['result_source_wikipedia']}, "
        f"ufcstats {report['fights_without_stats']['result_source_ufcstats']})",
        f"fighters with a completed fight: {report['fighters_with_a_completed_fight']}",
        f"fighters whose rates miss at least one fight: {gap['total']}",
        f"  only Wikipedia-sourced results: {gap['wikipedia_only']}",
        f"  only ufcstats-era fights with no stats: {gap['ufcstats_only']}",
        f"  both: {gap['both']}",
    ]
    since = report.get("since")
    if since:
        lines += [
            f"since {since['date']}:",
            f"  fighters with a completed fight: {since['fighters_with_a_completed_fight']}",
            f"  of those, a fight lacking stats: {since['fighters_with_a_fight_lacking_stats']}",
        ]
    return "\n".join(lines)
