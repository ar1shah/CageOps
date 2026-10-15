"""The SQL the API runs. Reads only.

"Completed" is the `completed_fights` view and nothing else (migration 0011): no query here
repeats its `status = 'completed'` predicate.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

from sqlalchemy import Connection, text

# A fighter's "division" is their most recent bout in one of these. Catch weight and open weight
# are one-off agreements, not a class a fighter belongs to, and a NULL class says nothing.
STANDARD_DIVISIONS = (
    "Flyweight",
    "Bantamweight",
    "Featherweight",
    "Lightweight",
    "Welterweight",
    "Middleweight",
    "Light Heavyweight",
    "Heavyweight",
    "Women's Strawweight",
    "Women's Flyweight",
    "Women's Bantamweight",
    "Women's Featherweight",
)


def escape_like(value: str) -> str:
    """Make `%`, `_` and the escape character itself literal in a LIKE pattern, so a search for
    `100%` is a search for those characters and `_` is not "any one character"."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# -- fighters ---------------------------------------------------------------------------------

_DIVISION = """
(SELECT cf.weight_class
   FROM completed_fights cf JOIN events ev ON ev.id = cf.event_id
  WHERE (cf.fighter_a_id = {id} OR cf.fighter_b_id = {id})
    AND cf.weight_class = ANY(:divisions)
  ORDER BY ev.event_date DESC, cf.id DESC LIMIT 1)
"""


def search_fighters(
    conn: Connection, query: str, *, limit: int, offset: int
) -> tuple[list[dict[str, Any]], int]:
    """(page of matches, total matches). Exact name first, then names starting with the query,
    then the rest, each by name and id so that paging is stable."""
    params = {
        "contains": f"%{escape_like(query)}%",
        "prefix": f"{escape_like(query)}%",
        "exact": query,
        "limit": limit,
        "offset": offset,
        "divisions": list(STANDARD_DIVISIONS),
    }
    total = conn.execute(
        text("SELECT count(*) FROM fighters WHERE name ILIKE :contains ESCAPE '\\'"), params
    ).scalar_one()
    rows = conn.execute(
        text(
            f"""
            WITH page AS (
              SELECT id, name FROM fighters
               WHERE name ILIKE :contains ESCAPE '\\'
               ORDER BY (lower(name) = lower(:exact)) DESC,
                        (name ILIKE :prefix ESCAPE '\\') DESC, name, id
               LIMIT :limit OFFSET :offset
            )
            SELECT p.id, p.name,
                   {_DIVISION.format(id="p.id")} AS weight_class,
                   (SELECT count(*) FROM completed_fights cf
                     WHERE cf.fighter_a_id = p.id OR cf.fighter_b_id = p.id) AS n_ufc_fights,
                   (SELECT max(ev.event_date) FROM completed_fights cf
                      JOIN events ev ON ev.id = cf.event_id
                     WHERE cf.fighter_a_id = p.id OR cf.fighter_b_id = p.id) AS last_fight_date
              FROM page p
             ORDER BY (lower(p.name) = lower(:exact)) DESC,
                      (p.name ILIKE :prefix ESCAPE '\\') DESC, p.name, p.id
            """
        ),
        params,
    ).mappings()
    return [dict(r) for r in rows], total


def fighter_profile(conn: Connection, fighter_id: int) -> dict[str, Any] | None:
    row = (
        conn.execute(
            text(
                f"""
            SELECT f.id, f.name, f.dob, f.height_cm, f.reach_cm, f.stance,
                   {_DIVISION.format(id="f.id")} AS weight_class
              FROM fighters f WHERE f.id = :id
            """
            ),
            {"id": fighter_id, "divisions": list(STANDARD_DIVISIONS)},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row else None


def fighter_record(conn: Connection, fighter_id: int) -> dict[str, int]:
    """Wins, losses and the rest from the fighter's own point of view."""
    row = (
        conn.execute(
            text(
                """
            SELECT
              count(*) FILTER (WHERE outcome = 'win' AND winner_id = :id) AS wins,
              count(*) FILTER (WHERE outcome = 'win' AND winner_id <> :id) AS losses,
              count(*) FILTER (WHERE outcome = 'draw') AS draws,
              count(*) FILTER (WHERE outcome = 'no_contest') AS no_contests,
              count(*) FILTER (WHERE outcome = 'unknown') AS unknown_results,
              count(*) FILTER (WHERE outcome = 'win' AND winner_id = :id
                                 AND method = 'ko_tko') AS wins_by_ko_tko,
              count(*) FILTER (WHERE outcome = 'win' AND winner_id = :id
                                 AND method = 'submission') AS wins_by_submission,
              count(*) FILTER (WHERE outcome = 'win' AND winner_id = :id
                                 AND method = 'decision') AS wins_by_decision,
              count(*) FILTER (WHERE outcome = 'win' AND winner_id = :id
                                 AND method NOT IN ('ko_tko', 'submission', 'decision')
                              ) AS wins_by_other
              FROM completed_fights
             WHERE fighter_a_id = :id OR fighter_b_id = :id
            """
            ),
            {"id": fighter_id},
        )
        .mappings()
        .one()
    )
    return {k: int(v) for k, v in row.items()}


def recent_fights(conn: Connection, fighter_id: int, limit: int = 5) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            """
            SELECT ev.event_date, ev.name AS event_name, cf.id AS fight_id,
                   opp.id AS opponent_id, opp.name AS opponent_name,
                   cf.outcome, cf.winner_id, cf.method, cf.finish_round,
                   cf.is_title_fight, cf.weight_class
              FROM completed_fights cf
              JOIN events ev ON ev.id = cf.event_id
              JOIN fighters opp
                ON opp.id = CASE WHEN cf.fighter_a_id = :id THEN cf.fighter_b_id
                                 ELSE cf.fighter_a_id END
             WHERE cf.fighter_a_id = :id OR cf.fighter_b_id = :id
             ORDER BY ev.event_date DESC, cf.id DESC
             LIMIT :limit
            """
        ),
        {"id": fighter_id, "limit": limit},
    ).mappings()
    return [dict(r) for r in rows]


# -- prediction inputs ------------------------------------------------------------------------


def fighter_names(conn: Connection, ids: Sequence[int]) -> dict[int, str]:
    rows = conn.execute(
        text("SELECT id, name FROM fighters WHERE id = ANY(:ids)"), {"ids": list(ids)}
    )
    return {r.id: r.name for r in rows}


def current_feature_rows(conn: Connection, ids: Sequence[int]) -> dict[int, dict[str, Any]]:
    rows = conn.execute(
        text("SELECT * FROM fighter_features_current WHERE fighter_id = ANY(:ids)"),
        {"ids": list(ids)},
    ).mappings()
    return {r["fighter_id"]: dict(r) for r in rows}


def recent_divisions(conn: Connection, ids: Sequence[int]) -> dict[int, str | None]:
    """Each fighter's most recent standard-division bout's class, or None."""
    rows = conn.execute(
        text(
            """
            SELECT DISTINCT ON (fid) fid, weight_class FROM (
              SELECT cf.fighter_a_id AS fid, cf.weight_class, ev.event_date, cf.id
                FROM completed_fights cf JOIN events ev ON ev.id = cf.event_id
              UNION ALL
              SELECT cf.fighter_b_id, cf.weight_class, ev.event_date, cf.id
                FROM completed_fights cf JOIN events ev ON ev.id = cf.event_id
            ) x
             WHERE fid = ANY(:ids) AND weight_class = ANY(:divisions)
             ORDER BY fid, event_date DESC, id DESC
            """
        ),
        {"ids": list(ids), "divisions": list(STANDARD_DIVISIONS)},
    )
    found = {r.fid: r.weight_class for r in rows}
    return {i: found.get(i) for i in ids}


def completed_fight_counts(conn: Connection, ids: Sequence[int], before: date) -> dict[int, int]:
    """How many completed fights each fighter has with an event date STRICTLY before `before`:
    the live number a stored feature row's `n_fights_career` must equal to be current (D-027,
    D-039)."""
    rows = conn.execute(
        text(
            """
            SELECT fid, count(*) AS n FROM (
              SELECT cf.fighter_a_id AS fid FROM completed_fights cf
                JOIN events ev ON ev.id = cf.event_id WHERE ev.event_date < :before
              UNION ALL
              SELECT cf.fighter_b_id FROM completed_fights cf
                JOIN events ev ON ev.id = cf.event_id WHERE ev.event_date < :before
            ) x WHERE fid = ANY(:ids) GROUP BY fid
            """
        ),
        {"ids": list(ids), "before": before},
    )
    found = {r.fid: int(r.n) for r in rows}
    return {i: found.get(i, 0) for i in ids}


# -- upcoming bouts ---------------------------------------------------------------------------


def scheduled_bouts(conn: Connection, today: date) -> list[dict[str, Any]]:
    """Every scheduled bout on an event dated today or later, in card order of the database
    (event date, then fight id). `fights` keeps the smaller fighter id first, so a bout's two
    sides are already in canonical order."""
    rows = conn.execute(
        text(
            """
            SELECT f.id AS fight_id, f.fighter_a_id, f.fighter_b_id,
                   f.weight_class, f.is_title_fight, f.scheduled_rounds,
                   ev.id AS event_id, ev.name AS event_name, ev.event_date
              FROM fights f JOIN events ev ON ev.id = f.event_id
             WHERE f.status = 'scheduled' AND ev.event_date >= :today
             ORDER BY ev.event_date, f.id
            """
        ),
        {"today": today},
    ).mappings()
    return [dict(r) for r in rows]


def bout_feature_rows(
    conn: Connection, fight_ids: Sequence[int]
) -> dict[tuple[int, int], dict[str, Any]]:
    """`fight_features` rows keyed (fight_id, fighter_id): each fighter's history as of the
    bout's own event date, written by the last features build."""
    rows = conn.execute(
        text("SELECT * FROM fight_features WHERE fight_id = ANY(:ids)"), {"ids": list(fight_ids)}
    ).mappings()
    return {(r["fight_id"], r["fighter_id"]): dict(r) for r in rows}


def completed_counts_before(
    conn: Connection, pairs: Sequence[tuple[int, date]]
) -> dict[tuple[int, date], int]:
    """For each (fighter, date): that fighter's completed fights strictly before the date.
    One round trip for any number of pairs."""
    if not pairs:
        return {}
    rows = conn.execute(
        text(
            """
            SELECT p.fid, p.d,
                   (SELECT count(*) FROM completed_fights cf
                      JOIN events ev ON ev.id = cf.event_id
                     WHERE (cf.fighter_a_id = p.fid OR cf.fighter_b_id = p.fid)
                       AND ev.event_date < p.d) AS n
              FROM unnest(CAST(:fids AS int[]), CAST(:dates AS date[])) AS p(fid, d)
            """
        ),
        {"fids": [p[0] for p in pairs], "dates": [p[1] for p in pairs]},
    )
    return {(r.fid, r.d): int(r.n) for r in rows}
