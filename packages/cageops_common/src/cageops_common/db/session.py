from sqlalchemy import Engine, create_engine


def make_engine(database_url: str, **kwargs) -> Engine:
    """Create an engine for a plain `postgresql://` URL (what .env.example uses).

    SQLAlchemy needs to be told which driver to use; we use psycopg 3. Extra keyword arguments go
    to create_engine (e.g. connect_args for a short connect timeout).
    """
    if database_url.startswith("postgresql://"):
        database_url = "postgresql+psycopg://" + database_url.removeprefix("postgresql://")
    return create_engine(database_url, **kwargs)
