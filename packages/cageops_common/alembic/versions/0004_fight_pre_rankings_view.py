"""fight_pre_rankings view: each fighter's ranking as known strictly before the fight

Rules (see D-011):
- Only snapshots dated strictly before the event date are used.
- If the latest snapshot is more than 21 days old, rank is NULL (status 'stale').
  The 21 must match cageops_common.db.rankings.RANKING_MAX_AGE_DAYS (a test checks this).
- Prefer a fresh snapshot from jerzyszocik, else a fresh one from martj42.
- `status` says why a rank is NULL: no_snapshot | stale | unranked.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LATEST_SNAPSHOT = """
    (SELECT '{source}'::text AS source, s.snapshot_date, {priority} AS priority
     FROM rankings s
     WHERE s.source = '{source}' AND s.ranking_type = l.ranking_type
       AND s.weight_class = l.list_class AND s.snapshot_date < l.event_date
     ORDER BY s.snapshot_date DESC LIMIT 1)
"""


def upgrade() -> None:
    latest = " UNION ALL ".join(
        _LATEST_SNAPSHOT.format(source=source, priority=priority)
        for priority, source in enumerate(("jerzyszocik", "martj42"))
    )
    op.create_index(
        "ix_rankings_list_snapshot",
        "rankings",
        ["source", "ranking_type", "weight_class", "snapshot_date"],
    )
    op.execute(
        f"""
        CREATE VIEW fight_pre_rankings AS
        SELECT l.fight_id, l.fighter_id, l.ranking_type, snap.source, snap.snapshot_date,
               (l.event_date - snap.snapshot_date) AS snapshot_age_days,
               CASE
                 WHEN snap.snapshot_date IS NULL THEN 'no_snapshot'
                 WHEN l.event_date - snap.snapshot_date > 21 THEN 'stale'
                 WHEN r.rank IS NULL THEN 'unranked'
                 ELSE 'ranked'
               END AS status,
               CASE WHEN l.event_date - snap.snapshot_date <= 21 THEN r.rank END AS rank
        FROM (
            SELECT f.id AS fight_id, e.event_date, p.fighter_id, t.ranking_type,
                   CASE t.ranking_type
                     WHEN 'division' THEN f.weight_class
                     WHEN 'pound_for_pound' THEN
                       CASE f.gender WHEN 'M' THEN 'Men''s Pound-for-Pound'
                                     ELSE 'Women''s Pound-for-Pound' END
                   END AS list_class
            FROM fights f
            JOIN events e ON e.id = f.event_id
            CROSS JOIN LATERAL (VALUES (f.fighter_a_id), (f.fighter_b_id)) AS p(fighter_id)
            CROSS JOIN (VALUES ('division'), ('pound_for_pound')) AS t(ranking_type)
        ) l
        LEFT JOIN LATERAL (
            SELECT c.source, c.snapshot_date
            FROM ({latest}) c
            ORDER BY (l.event_date - c.snapshot_date > 21), c.priority
            LIMIT 1
        ) snap ON true
        LEFT JOIN rankings r
               ON r.source = snap.source AND r.snapshot_date = snap.snapshot_date
              AND r.ranking_type = l.ranking_type AND r.weight_class = l.list_class
              AND r.fighter_id = l.fighter_id
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW fight_pre_rankings")
    op.drop_index("ix_rankings_list_snapshot", table_name="rankings")
