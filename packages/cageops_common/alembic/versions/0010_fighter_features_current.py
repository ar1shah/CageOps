"""fighter_features_current: one row per fighter, as of a build date (D-037)

`fight_features` is as-of each *fight's* date, so no row in it describes a fighter "today": a
fighter's latest row leaves out their latest fight. An ad-hoc prediction needs exactly that.
This table is written by `features rebuild` in the same transaction as `fight_features` (same
`load_run_id`), from the same history, and read by the API.

It stores the history-derived columns plus the *inputs* to the two calendar-dependent features
(`dob`, `last_fight_date`) and deliberately has no `age_days` or `days_since_last_fight`: stored,
they would be wrong a day later with nothing to say so. They are derived at request time by
`cageops_common.inference.asof.date_relative_features`, the same function the builder calls.

No `weight_class`, `is_title_fight` or `scheduled_rounds`: those belong to a fight, and an ad-hoc
matchup supplies its own.

Revision ID: 0010
Revises: 0009
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen here on purpose: a migration must not change when application code does.
STATS = (
    "sig_str_landed_pm",
    "sig_str_absorbed_pm",
    "sig_str_acc",
    "sig_str_def",
    "td_landed_per15",
    "td_def",
    "sub_att_per15",
    "kd_per15",
)
WINDOWS = ("career", "last3", "last5")


def upgrade() -> None:
    columns: list[sa.Column] = [
        sa.Column("fighter_id", sa.Integer(), nullable=False),
        # Completed fights strictly before this date are the history the row counts (D-027).
        sa.Column("built_as_of", sa.Date(), nullable=False),
        sa.Column("last_fight_date", sa.Date(), nullable=True),
        sa.Column("dob", sa.Date(), nullable=True),
    ]
    for window in WINDOWS:
        columns += [sa.Column(f"{stat}_{window}", sa.Double(), nullable=True) for stat in STATS]
        columns += [
            sa.Column(f"n_fights_{window}", sa.Integer(), nullable=False),
            sa.Column(f"n_fights_with_stats_{window}", sa.Integer(), nullable=False),
            sa.Column(f"n_fights_with_duration_{window}", sa.Integer(), nullable=False),
        ]
    columns += [
        sa.Column("prior_ufc_fights", sa.Integer(), nullable=False),
        sa.Column("win_streak", sa.Integer(), nullable=True),
        sa.Column("finish_rate", sa.Double(), nullable=True),
        sa.Column("height_cm", sa.Double(), nullable=True),
        sa.Column("reach_cm", sa.Double(), nullable=True),
        sa.Column("stance", sa.Text(), nullable=True),
        sa.Column("has_unresolved_prior_fight", sa.Boolean(), nullable=False),
        sa.Column(
            "built_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("load_run_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["fighter_id"], ["fighters.id"]),
        sa.ForeignKeyConstraint(["load_run_id"], ["load_runs.id"]),
        sa.PrimaryKeyConstraint("fighter_id"),
    ]
    op.create_table("fighter_features_current", *columns)


def downgrade() -> None:
    op.drop_table("fighter_features_current")
