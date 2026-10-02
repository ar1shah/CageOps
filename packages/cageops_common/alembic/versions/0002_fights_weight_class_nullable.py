"""fights weight_class nullable

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-02 15:37:27.593656
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("fights", "weight_class", existing_type=sa.TEXT(), nullable=True)


def downgrade() -> None:
    op.alter_column("fights", "weight_class", existing_type=sa.TEXT(), nullable=False)
