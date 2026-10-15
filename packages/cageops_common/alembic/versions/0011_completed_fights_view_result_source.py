"""completed_fights: expose result_source, with an explicit column list

0006 created the view as `SELECT * FROM fights WHERE status = 'completed'`. Postgres expands `*`
once, at creation, so the view silently stopped tracking `fights` and never got `result_source`
(added in 0008). Nothing decided that: D-029 only noted it. Code that needs to know who supplied a
result (`features stats-gap`, the API's freshness check) had to bypass the view and repeat its
`status = 'completed'` predicate, which is two definitions of "completed".

This replaces the view so there is one: the columns it had, in the same order, then
`result_source`. `CREATE OR REPLACE VIEW` accepts that only because the new column is appended, so
every existing reader (all of which name their columns) is unaffected. The list is explicit
this time, so a column added to `fights` later does not appear here by accident; a test fails
until someone decides.

Revision ID: 0011
Revises: 0010
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Order matters: CREATE OR REPLACE VIEW only allows appending columns to an existing view.
OLD_COLUMNS = (
    "id",
    "ufcstats_id",
    "event_id",
    "fighter_a_id",
    "fighter_b_id",
    "red_fighter_id",
    "weight_class",
    "gender",
    "is_title_fight",
    "scheduled_rounds",
    "outcome",
    "winner_id",
    "method",
    "decision_type",
    "method_detail",
    "finish_round",
    "finish_time_sec",
    "referee",
    "has_round_stats",
    "status",
)
NEW_COLUMNS = (*OLD_COLUMNS, "result_source")


def _view(columns: tuple[str, ...]) -> str:
    return f"SELECT {', '.join(columns)} FROM fights WHERE status = 'completed'"  # noqa: S608


def upgrade() -> None:
    op.execute(f"CREATE OR REPLACE VIEW completed_fights AS {_view(NEW_COLUMNS)}")


def downgrade() -> None:
    # A view cannot lose a column through CREATE OR REPLACE.
    op.execute("DROP VIEW completed_fights")
    op.execute(f"CREATE VIEW completed_fights AS {_view(OLD_COLUMNS)}")
