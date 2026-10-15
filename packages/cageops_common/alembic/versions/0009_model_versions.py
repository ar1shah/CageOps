"""Model registry: model_versions, with at most one active version enforced by the database (D-033)

One row is one trained bundle (the win/loss and method models together), so activating a version
is a single switch and a prediction's `model_version` is one string.

`is_active` is guarded by a partial unique index: Postgres rejects a second active row, whatever
the application does. Switching versions therefore deactivates the old one first, in the same
transaction, because a unique index is checked row by row.

Revision ID: 0009
Revises: 0008
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_versions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("trained_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data_cutoff", sa.Date(), nullable=False),
        sa.Column("features_output_sha256", sa.String(64), nullable=False),
        sa.Column("features_load_run_id", sa.BigInteger(), nullable=False),
        sa.Column("metrics", postgresql.JSONB(), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("artifact_path", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="false", nullable=False),
        sa.ForeignKeyConstraint(["features_load_run_id"], ["load_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version"),
    )
    op.create_index(
        "uq_model_versions_one_active",
        "model_versions",
        ["is_active"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )


def downgrade() -> None:
    op.drop_index("uq_model_versions_one_active", table_name="model_versions")
    op.drop_table("model_versions")
