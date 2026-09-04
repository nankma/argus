"""
Storage backend selection. get_storage() returns a process-wide singleton
-- SqliteStorage or PostgresStorage, picked by storage.database.type --
that every *_ops.py module (subscriber_ops.py, category_ops.py, ...) calls
into. Same "one settings-selected implementation, built once" shape as
telemetry.setup_telemetry(), just for a single active backend instead of
a list of simultaneously-active ones.
"""

from app_settings import get_settings
from storage.engine import build_engine

_storage = None


def get_storage():
    global _storage
    if _storage is None:
        _storage = _build_storage()
    return _storage


def _build_storage():
    db_type = get_settings().resolved("storage.database.type", default="sqlite")
    engine = build_engine()
    if db_type == "sqlite":
        from storage.sqlite import SqliteStorage
        return SqliteStorage(engine)
    if db_type == "postgres":
        from storage.postgres import PostgresStorage
        return PostgresStorage(engine)
    raise ValueError(f"storage.database.type={db_type!r} is not a recognized backend")


def init_db() -> None:
    """Schema-only startup (create tables, additive migrations). Callers
    also need category_ops.bootstrap() for the taxonomy's actual seed
    content -- see storage/sqlite/__init__.py's SqliteStorage.init_db
    docstring for why that's split out."""
    get_storage().init_db()


def reset_storage_for_tests(storage=None) -> None:
    """Test-only. No-arg call forces the next get_storage() to rebuild
    from settings/env; pass a Storage instance (e.g. SqliteStorage wrapping
    an in-memory or temp-file engine) to inject a fake for the duration of
    a test. Mirrors app_settings.reset_settings_for_tests."""
    global _storage
    _storage = storage
