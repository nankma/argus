"""
Builds the SQLAlchemy Engine for whichever backend `storage.database.type`
selects. One engine per process, built once (see storage/__init__.py's
get_storage()) -- SQLAlchemy engines already pool connections internally,
so there's no reason for callers to build their own.
"""

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from app_settings import get_settings


def build_engine() -> Engine:
    db_type = get_settings().resolved("storage.database.type", default="sqlite")
    if db_type == "sqlite":
        path = get_settings().resolved("storage.database.sqlite.path", required=True)
        return create_engine(f"sqlite:///{path}")
    if db_type == "postgres":
        host = get_settings().resolved("storage.database.postgres.host", required=True)
        port = get_settings().resolved("storage.database.postgres.port", default=5432)
        dbname = get_settings().resolved("storage.database.postgres.dbname", required=True)
        user = get_settings().resolved("storage.database.postgres.user", required=True)
        password = get_settings().resolved("storage.database.postgres.password", required=True)
        return create_engine(
            f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{dbname}"
        )
    raise ValueError(f"storage.database.type={db_type!r} is not a recognized backend")
