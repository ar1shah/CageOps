"""fight_features: point-in-time features, one row per (fight, fighter)

Derived and rebuildable (D-027). Foreign keys point at fights, fighters and the load run that
built the row. No default on `fight_id`/`fighter_id`: a feature row is always about a real
fighter in a real fight.

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fight_features",
        sa.Column("fight_id", sa.Integer(), nullable=False),
        sa.Column("fighter_id", sa.Integer(), nullable=False),
        sa.Column("sig_str_landed_pm_career", sa.Double(), nullable=True),
        sa.Column("sig_str_absorbed_pm_career", sa.Double(), nullable=True),
        sa.Column("sig_str_acc_career", sa.Double(), nullable=True),
        sa.Column("sig_str_def_career", sa.Double(), nullable=True),
        sa.Column("td_landed_per15_career", sa.Double(), nullable=True),
        sa.Column("td_def_career", sa.Double(), nullable=True),
        sa.Column("sub_att_per15_career", sa.Double(), nullable=True),
        sa.Column("kd_per15_career", sa.Double(), nullable=True),
        sa.Column("n_fights_career", sa.Integer(), nullable=False),
        sa.Column("n_fights_with_stats_career", sa.Integer(), nullable=False),
        sa.Column("n_fights_with_duration_career", sa.Integer(), nullable=False),
        sa.Column("sig_str_landed_pm_last3", sa.Double(), nullable=True),
        sa.Column("sig_str_absorbed_pm_last3", sa.Double(), nullable=True),
        sa.Column("sig_str_acc_last3", sa.Double(), nullable=True),
        sa.Column("sig_str_def_last3", sa.Double(), nullable=True),
        sa.Column("td_landed_per15_last3", sa.Double(), nullable=True),
        sa.Column("td_def_last3", sa.Double(), nullable=True),
        sa.Column("sub_att_per15_last3", sa.Double(), nullable=True),
        sa.Column("kd_per15_last3", sa.Double(), nullable=True),
        sa.Column("n_fights_last3", sa.Integer(), nullable=False),
        sa.Column("n_fights_with_stats_last3", sa.Integer(), nullable=False),
        sa.Column("n_fights_with_duration_last3", sa.Integer(), nullable=False),
        sa.Column("sig_str_landed_pm_last5", sa.Double(), nullable=True),
        sa.Column("sig_str_absorbed_pm_last5", sa.Double(), nullable=True),
        sa.Column("sig_str_acc_last5", sa.Double(), nullable=True),
        sa.Column("sig_str_def_last5", sa.Double(), nullable=True),
        sa.Column("td_landed_per15_last5", sa.Double(), nullable=True),
        sa.Column("td_def_last5", sa.Double(), nullable=True),
        sa.Column("sub_att_per15_last5", sa.Double(), nullable=True),
        sa.Column("kd_per15_last5", sa.Double(), nullable=True),
        sa.Column("n_fights_last5", sa.Integer(), nullable=False),
        sa.Column("n_fights_with_stats_last5", sa.Integer(), nullable=False),
        sa.Column("n_fights_with_duration_last5", sa.Integer(), nullable=False),
        sa.Column("prior_ufc_fights", sa.Integer(), nullable=False),
        sa.Column("win_streak", sa.Integer(), nullable=True),
        sa.Column("finish_rate", sa.Double(), nullable=True),
        sa.Column("days_since_last_fight", sa.Integer(), nullable=True),
        sa.Column("age_days", sa.Integer(), nullable=True),
        sa.Column("height_cm", sa.Double(), nullable=True),
        sa.Column("reach_cm", sa.Double(), nullable=True),
        sa.Column("stance", sa.Text(), nullable=True),
        sa.Column("weight_class", sa.Text(), nullable=True),
        sa.Column("is_title_fight", sa.Boolean(), nullable=True),
        sa.Column("scheduled_rounds", sa.SmallInteger(), nullable=True),
        sa.Column("has_unresolved_prior_fight", sa.Boolean(), nullable=False),
        sa.Column(
            "built_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("load_run_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["fight_id"], ["fights.id"]),
        sa.ForeignKeyConstraint(["fighter_id"], ["fighters.id"]),
        sa.ForeignKeyConstraint(["load_run_id"], ["load_runs.id"]),
        sa.PrimaryKeyConstraint("fight_id", "fighter_id"),
    )


def downgrade() -> None:
    op.drop_table("fight_features")
