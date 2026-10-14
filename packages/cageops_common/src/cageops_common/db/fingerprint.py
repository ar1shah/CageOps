"""One hash of everything that can change a training feature or a training label (D-033).

`fight_features` is derived data. The features builder stores this hash next to its output; the
trainer recomputes it before training. If the two differ, a fight, a bio or a stat the features
were built from has changed since, and training on them would train on stale features.

Scope is deliberately narrow: the COMPLETED fights, their date, their two fighters' bios, and
their whole-fight stats. Nothing else can change a training row, so nothing else is hashed:
scheduled and cancelled bouts, fighters or events with no completed fight, odds, rankings,
raw pages and load runs. A scheduled scrape that only churns an upcoming card therefore never
makes the features look stale.

The column lists below are the single source of truth: the SQL is generated from INCLUDED, and
every column of the four tables must be classified (INCLUDED or EXCLUDED, with a reason) or a test
fails. Adding a column to one of these tables forces a decision here.
"""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import Connection, text

# Bump this to deliberately invalidate every stored hash (a changed column list does it anyway).
FINGERPRINT_VERSION = "inputs-v1"

# What the hash is made of. "fights" rows come through `completed_fights`, the same door the
# builder uses (D-023), joined to `events` for the date.
INCLUDED: dict[str, tuple[str, ...]] = {
    "fights": (
        "id",
        "event_id",  # so a fight moved between events is seen
        "fighter_a_id",
        "fighter_b_id",
        "outcome",
        "winner_id",
        "method",
        "scheduled_rounds",
        "finish_round",
        "finish_time_sec",
        "weight_class",
        "is_title_fight",
    ),
    "events": ("event_date",),
    "fighters": ("id", "dob", "height_cm", "reach_cm", "stance"),
    "fight_totals": (
        "fight_id",
        "fighter_id",
        "sig_strikes_landed",
        "sig_strikes_att",
        "takedowns_landed",
        "takedowns_att",
        "submission_att",
        "knockdowns",
    ),
}

# Every other column, and why it cannot change a feature or a label.
EXCLUDED: dict[str, dict[str, str]] = {
    "fights": {
        "ufcstats_id": "identity key, not data",
        "red_fighter_id": "corners are not features",
        "gender": "not read by the features builder",
        "status": "set membership: a fight that leaves 'completed' drops out of the hashed rows",
        "result_source": "who supplied the result; the same result from another source changes "
        "no feature or label",
        "decision_type": "not read by the features builder",
        "method_detail": "prose, never stored for Wikipedia; not read",
        "referee": "not read by the features builder",
        "has_round_stats": "derived from stats rows, which are hashed; not read",
    },
    "events": {
        "id": "carried by fights.event_id",
        "ufcstats_id": "identity key, not data",
        "wikipedia_article_id": "identity key, not data",
        "name": "display only",
        "city": "display only",
        "state": "display only",
        "country": "display only",
    },
    "fighters": {
        "ufcstats_id": "identity key, not data",
        "wikipedia_title": "identity key, not data (a link edit changes no feature)",
        "name": "display only; no feature reads a name",
    },
    "fight_totals": {
        "total_strikes_landed": "not read by the features builder",
        "total_strikes_att": "not read by the features builder",
        "reversals": "not read by the features builder",
        "ctrl_sec": "not read by the features builder",
    },
}

# Whole tables left out.
EXCLUDED_TABLES: dict[str, str] = {
    "fight_round_stats": "the builder reads fight_totals only (D-027)",
}


def _columns(alias: str, table: str) -> str:
    return ", ".join(f"{alias}.{c}" for c in INCLUDED[table])


# Every statement orders by primary key so the hash never depends on physical row order.
_QUERIES: tuple[tuple[str, str], ...] = (
    (
        "fights",
        f"SELECT {_columns('f', 'fights')}, {_columns('e', 'events')}"
        " FROM completed_fights f JOIN events e ON e.id = f.event_id ORDER BY f.id",
    ),
    (
        "fighters",
        f"SELECT {_columns('p', 'fighters')} FROM fighters p WHERE p.id IN ("
        "SELECT fighter_a_id FROM completed_fights UNION SELECT fighter_b_id FROM completed_fights"
        ") ORDER BY p.id",
    ),
    (
        "fight_totals",
        f"SELECT {_columns('t', 'fight_totals')} FROM fight_totals t"
        " WHERE t.fight_id IN (SELECT id FROM completed_fights)"
        " ORDER BY t.fight_id, t.fighter_id",
    ),
)


def features_input_fingerprint(conn: Connection) -> str:
    """sha256 over the rows described above. Read inside the caller's transaction."""
    digest = hashlib.sha256(FINGERPRINT_VERSION.encode())
    for name, sql in _QUERIES:
        rows = [list(row) for row in conn.execute(text(sql))]
        columns = INCLUDED[name] + (INCLUDED["events"] if name == "fights" else ())
        payload = json.dumps(
            {"table": name, "columns": columns, "rows": rows},
            default=str,  # dates; floats and ints are already JSON
            separators=(",", ":"),
        )
        digest.update(payload.encode())
    return digest.hexdigest()
