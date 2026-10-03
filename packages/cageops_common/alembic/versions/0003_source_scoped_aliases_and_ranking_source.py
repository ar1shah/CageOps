"""source-scoped aliases and a source column on rankings

Both tables are empty when this runs (nothing was loaded into them yet), so they are
rebuilt rather than altered: the primary keys change shape.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("fighter_aliases")
    op.create_table(
        "fighter_aliases",
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("alias_norm", sa.Text(), nullable=False),
        sa.Column("fighter_id", sa.Integer(), sa.ForeignKey("fighters.id"), nullable=False),
        sa.PrimaryKeyConstraint("source", "alias_norm"),
    )
    op.drop_table("rankings")
    op.create_table(
        "rankings",
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("ranking_type", sa.Text(), nullable=False),
        sa.Column("weight_class", sa.Text(), nullable=False),
        sa.Column("name_raw", sa.Text(), nullable=False),
        sa.Column("fighter_id", sa.Integer(), sa.ForeignKey("fighters.id"), nullable=True),
        sa.Column("rank", sa.SmallInteger(), nullable=False),
        sa.PrimaryKeyConstraint(
            "source", "snapshot_date", "ranking_type", "weight_class", "name_raw"
        ),
        sa.CheckConstraint("source IN ('jerzyszocik', 'martj42')", name="rankings_source_values"),
        sa.CheckConstraint(
            "ranking_type IN ('division', 'pound_for_pound')", name="ranking_type_values"
        ),
        sa.CheckConstraint("rank >= 0", name="rank_non_negative"),
    )
    op.create_index("ix_rankings_fighter_snapshot", "rankings", ["fighter_id", "snapshot_date"])


def downgrade() -> None:
    op.drop_table("rankings")
    op.create_table(
        "rankings",
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("ranking_type", sa.Text(), nullable=False),
        sa.Column("weight_class", sa.Text(), nullable=False),
        sa.Column("name_raw", sa.Text(), nullable=False),
        sa.Column("fighter_id", sa.Integer(), sa.ForeignKey("fighters.id"), nullable=True),
        sa.Column("rank", sa.SmallInteger(), nullable=False),
        sa.PrimaryKeyConstraint("snapshot_date", "ranking_type", "weight_class", "name_raw"),
        sa.CheckConstraint(
            "ranking_type IN ('division', 'pound_for_pound')", name="ranking_type_values"
        ),
        sa.CheckConstraint("rank >= 0", name="rank_non_negative"),
    )
    op.create_index("ix_rankings_fighter_snapshot", "rankings", ["fighter_id", "snapshot_date"])
    op.drop_table("fighter_aliases")
    op.create_table(
        "fighter_aliases",
        sa.Column("alias_norm", sa.Text(), primary_key=True),
        sa.Column("fighter_id", sa.Integer(), sa.ForeignKey("fighters.id"), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
    )
