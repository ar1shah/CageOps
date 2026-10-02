from alembic import context
from sqlalchemy import Connection

from cageops_common.config import get_settings
from cageops_common.db.models import Base
from cageops_common.db.session import make_engine

config = context.config
target_metadata = Base.metadata


def run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


def main() -> None:
    # Tests pass a ready connection; normal runs build one from DATABASE_URL.
    connection = config.attributes.get("connection")
    if connection is not None:
        run_migrations(connection)
        return
    engine = make_engine(get_settings().database_url)
    with engine.connect() as conn:
        run_migrations(conn)
        conn.commit()


main()
