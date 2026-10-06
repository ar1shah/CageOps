"""Second results source: per-source keys, fights.result_source, a natural key for fights (D-029)

ufcstats ids stay the only thing allowed in `ufcstats_id` (16 lowercase hex, now a CHECK), so the
column becomes nullable instead of holding made-up ids for rows another source created. Those rows
are identified by their own source's key:
- events.wikipedia_article_id (the article's id, survives a rename)
- fighters.wikipedia_title (the /wiki/ link target, survives a display-name edit)
- fights have no source key; their identity is (event, fighter pair), now UNIQUE.
fights.result_source says whose result a completed fight carries, so a ufcstats result can
replace a Wikipedia one (and never the other way round). Every existing completed fight is
'ufcstats'.

Revision ID: 0008
Revises: 0007
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

HEX16 = "ufcstats_id ~ '^[0-9a-f]{16}$'"


def upgrade() -> None:
    for table in ("events", "fighters", "fights"):
        op.alter_column(table, "ufcstats_id", existing_type=sa.String(32), nullable=True)
        op.create_check_constraint("ufcstats_id_format", table, HEX16)

    op.add_column("events", sa.Column("wikipedia_article_id", sa.BigInteger(), nullable=True))
    op.create_unique_constraint(
        "events_wikipedia_article_id_key", "events", ["wikipedia_article_id"]
    )
    op.create_check_constraint(
        "has_a_source_key", "events", "ufcstats_id IS NOT NULL OR wikipedia_article_id IS NOT NULL"
    )

    op.add_column("fighters", sa.Column("wikipedia_title", sa.Text(), nullable=True))
    op.create_unique_constraint("fighters_wikipedia_title_key", "fighters", ["wikipedia_title"])
    op.create_check_constraint(
        "has_a_source_key", "fighters", "ufcstats_id IS NOT NULL OR wikipedia_title IS NOT NULL"
    )

    op.add_column("fights", sa.Column("result_source", sa.Text(), nullable=True))
    op.execute("UPDATE fights SET result_source = 'ufcstats' WHERE status = 'completed'")
    op.create_check_constraint(
        "result_source_values", "fights", "result_source IN ('ufcstats', 'wikipedia')"
    )
    op.create_check_constraint(
        "result_source_iff_completed",
        "fights",
        "(status = 'completed') = (result_source IS NOT NULL)",
    )
    op.create_unique_constraint(
        "uq_fights_event_pair", "fights", ["event_id", "fighter_a_id", "fighter_b_id"]
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table in ("events", "fighters", "fights"):
        keyless = bind.execute(
            sa.text(f"SELECT count(*) FROM {table} WHERE ufcstats_id IS NULL")
        ).scalar_one()
        if keyless:
            raise RuntimeError(
                f"cannot downgrade: {keyless} {table} rows have no ufcstats_id (they came from "
                "another source) and would violate the old NOT NULL column; delete them first"
            )
    op.drop_constraint("uq_fights_event_pair", "fights", type_="unique")
    op.drop_constraint("result_source_iff_completed", "fights", type_="check")
    op.drop_constraint("result_source_values", "fights", type_="check")
    op.drop_column("fights", "result_source")

    op.drop_constraint("has_a_source_key", "fighters", type_="check")
    op.drop_constraint("fighters_wikipedia_title_key", "fighters", type_="unique")
    op.drop_column("fighters", "wikipedia_title")

    op.drop_constraint("has_a_source_key", "events", type_="check")
    op.drop_constraint("events_wikipedia_article_id_key", "events", type_="unique")
    op.drop_column("events", "wikipedia_article_id")

    for table in ("events", "fighters", "fights"):
        op.drop_constraint("ufcstats_id_format", table, type_="check")
        op.alter_column(table, "ufcstats_id", existing_type=sa.String(32), nullable=False)
