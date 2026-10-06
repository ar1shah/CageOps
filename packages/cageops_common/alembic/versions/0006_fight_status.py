"""fights.status: scheduled / completed / cancelled bouts (D-023)

The seed only ever held fights that already happened. The live scraper also sees upcoming bouts,
and bouts that get removed from a card. So:
- fights.status says which of the three it is. Every existing row becomes 'completed'.
- outcome, method and is_title_fight become nullable: a bout with no result has no outcome (NULL,
  not 'unknown', which keeps meaning "a completed fight whose result the seed couldn't
  determine"), no method, and an unknown title flag (never a made-up false).
- SQL CHECKs pass when their condition is NULL, so the old win_has_winner would have let a winner
  onto a scheduled row (outcome NULL makes the whole condition NULL). It is rewritten NULL-safe.
- completed_fights is the one door for the feature pipeline and grading: only finished fights.

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "fights", sa.Column("status", sa.Text(), server_default="completed", nullable=False)
    )
    op.create_check_constraint(
        "status_values", "fights", "status IN ('scheduled', 'completed', 'cancelled')"
    )
    op.alter_column("fights", "outcome", existing_type=sa.Text(), nullable=True)
    op.alter_column("fights", "method", existing_type=sa.Text(), nullable=True)
    op.alter_column("fights", "is_title_fight", existing_type=sa.Boolean(), nullable=True)

    op.drop_constraint("win_has_winner", "fights", type_="check")
    op.create_check_constraint(
        "win_has_winner", "fights", "(outcome IS NOT DISTINCT FROM 'win') = (winner_id IS NOT NULL)"
    )
    op.create_check_constraint(
        "completed_has_result",
        "fights",
        "status <> 'completed' OR "
        "(outcome IS NOT NULL AND method IS NOT NULL AND is_title_fight IS NOT NULL)",
    )
    op.create_check_constraint(
        "unfought_has_no_result",
        "fights",
        "status = 'completed' OR "
        "(outcome IS NULL AND method IS NULL AND winner_id IS NULL AND NOT has_round_stats)",
    )
    op.execute("CREATE VIEW completed_fights AS SELECT * FROM fights WHERE status = 'completed'")


def downgrade() -> None:
    unfinished = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM fights WHERE status <> 'completed'"))
        .scalar_one()
    )
    if unfinished:
        raise RuntimeError(
            f"cannot downgrade: {unfinished} scheduled or cancelled fights have no outcome or "
            "method and would violate the old NOT NULL columns; delete them first"
        )
    op.execute("DROP VIEW completed_fights")
    op.drop_constraint("unfought_has_no_result", "fights", type_="check")
    op.drop_constraint("completed_has_result", "fights", type_="check")
    op.drop_constraint("win_has_winner", "fights", type_="check")
    op.create_check_constraint(
        "win_has_winner", "fights", "(outcome = 'win') = (winner_id IS NOT NULL)"
    )
    op.alter_column("fights", "is_title_fight", existing_type=sa.Boolean(), nullable=False)
    op.alter_column("fights", "method", existing_type=sa.Text(), nullable=False)
    op.alter_column("fights", "outcome", existing_type=sa.Text(), nullable=False)
    op.drop_constraint("status_values", "fights", type_="check")
    op.drop_column("fights", "status")
