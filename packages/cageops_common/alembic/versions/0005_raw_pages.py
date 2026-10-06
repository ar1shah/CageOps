"""raw_pages: cache of fetched HTML, one row per URL (latest copy)

See D-015 for when a cached copy counts as fresh.

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "raw_pages",
        sa.Column("url", sa.Text(), primary_key=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.SmallInteger(), nullable=False),
        sa.Column("html", sa.Text(), nullable=False),
        sa.CheckConstraint("status IN (200, 404)", name="raw_page_status_values"),
    )


def downgrade() -> None:
    op.drop_table("raw_pages")
