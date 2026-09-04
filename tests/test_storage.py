"""
Tests for storage/__init__.py's backend-selection dispatch and
storage/engine.py's engine construction -- the actual "type: sqlite vs
type: postgres" seam this whole refactor exists for. Deliberately
separate from tests/conftest.py's isolated_subscribers_db fixture, which
injects a pre-built SqliteStorage directly and so never exercises this
dispatch path at all.

No live Postgres server needed anywhere here -- SQLAlchemy's
create_engine() never connects eagerly, so a postgresql+psycopg2:// URL
can be built and inspected without a server to point it at.
"""

import pytest
from trailsign import Settings

import app_settings
import storage
from storage.engine import build_engine
from storage.postgres import PostgresStorage
from storage.sqlite import SqliteStorage


@pytest.fixture
def _restore_settings_and_storage():
    """Saves whatever Settings conftest.py's base fixture installed, and
    restores exactly that object afterward -- forcing a disk reload
    instead (reset_settings_for_tests(None)) would risk picking up a real
    settings.yml for whichever test runs next."""
    original = app_settings.get_settings()
    yield
    app_settings.reset_settings_for_tests(original)
    storage.reset_storage_for_tests(None)


def _settings_with_database(database: dict) -> Settings:
    return Settings({"storage": {"database": database}})


def test_build_engine_sqlite(tmp_path, _restore_settings_and_storage):
    app_settings.reset_settings_for_tests(_settings_with_database({
        "type": "sqlite", "sqlite": {"path": str(tmp_path / "x.db")},
    }))
    engine = build_engine()
    assert engine.dialect.name == "sqlite"


def test_build_engine_postgres_builds_the_url_without_connecting(_restore_settings_and_storage):
    app_settings.reset_settings_for_tests(_settings_with_database({
        "type": "postgres",
        "postgres": {"host": "dbhost", "port": 5433, "dbname": "mydb", "user": "u", "password": "p"},
    }))
    engine = build_engine()
    assert engine.dialect.name == "postgresql"
    assert engine.url.host == "dbhost"
    assert engine.url.port == 5433
    assert engine.url.database == "mydb"
    assert engine.url.username == "u"
    assert engine.url.password == "p"


def test_build_engine_postgres_defaults_the_port(_restore_settings_and_storage):
    app_settings.reset_settings_for_tests(_settings_with_database({
        "type": "postgres",
        "postgres": {"host": "dbhost", "dbname": "mydb", "user": "u", "password": "p"},
    }))
    assert build_engine().url.port == 5432


def test_build_engine_unknown_type_raises(_restore_settings_and_storage):
    app_settings.reset_settings_for_tests(_settings_with_database({"type": "mongo"}))
    with pytest.raises(ValueError):
        build_engine()


def test_get_storage_dispatches_to_sqlite(tmp_path, _restore_settings_and_storage):
    app_settings.reset_settings_for_tests(_settings_with_database({
        "type": "sqlite", "sqlite": {"path": str(tmp_path / "x.db")},
    }))
    storage.reset_storage_for_tests(None)
    assert type(storage.get_storage()) is SqliteStorage


def test_get_storage_dispatches_to_postgres(_restore_settings_and_storage):
    app_settings.reset_settings_for_tests(_settings_with_database({
        "type": "postgres",
        "postgres": {"host": "dbhost", "dbname": "mydb", "user": "u", "password": "p"},
    }))
    storage.reset_storage_for_tests(None)
    # type(), not isinstance() -- PostgresStorage subclasses SqliteStorage
    # (see storage/postgres/__init__.py), so isinstance alone wouldn't
    # distinguish "dispatched to postgres" from "dispatched to sqlite".
    assert type(storage.get_storage()) is PostgresStorage


def test_get_storage_unknown_type_raises(_restore_settings_and_storage):
    app_settings.reset_settings_for_tests(_settings_with_database({"type": "mongo"}))
    storage.reset_storage_for_tests(None)
    with pytest.raises(ValueError):
        storage.get_storage()


def test_get_storage_is_a_singleton_within_one_reset_cycle(tmp_path, _restore_settings_and_storage):
    app_settings.reset_settings_for_tests(_settings_with_database({
        "type": "sqlite", "sqlite": {"path": str(tmp_path / "x.db")},
    }))
    storage.reset_storage_for_tests(None)
    assert storage.get_storage() is storage.get_storage()


# --- PostgresStorage's own overrides ---------------------------------------
#
# __new__ (not the constructor) -- these two methods don't touch
# self._engine at all, so no engine (real or fake) is needed to exercise
# them directly.


def test_postgres_insert_ignore_uses_on_conflict_do_nothing():
    pg = PostgresStorage.__new__(PostgresStorage)
    assert pg._insert_ignore_prefix("categories") == "INSERT INTO categories"
    assert pg._on_conflict_nothing(["name"]) == "ON CONFLICT (name) DO NOTHING"
    assert pg._on_conflict_nothing(["chat_id", "topic"]) == "ON CONFLICT (chat_id, topic) DO NOTHING"


def test_sqlite_insert_ignore_differs_from_postgres():
    sq = SqliteStorage.__new__(SqliteStorage)
    assert sq._insert_ignore_prefix("categories") == "INSERT OR IGNORE INTO categories"
    assert sq._on_conflict_nothing(["name"]) == ""  # OR IGNORE in the prefix already does the job


def test_postgres_overrides_list_columns_and_conflict_primitives():
    """A regression guard for the override itself, not its introspection
    logic (which needs a real Postgres connection to test meaningfully) --
    confirms PostgresStorage doesn't silently fall back to SqliteStorage's
    PRAGMA-based _list_columns if someone edits storage/postgres/__init__.py
    later."""
    assert PostgresStorage._list_columns is not SqliteStorage._list_columns
    assert PostgresStorage._insert_ignore_prefix is not SqliteStorage._insert_ignore_prefix
    assert PostgresStorage._on_conflict_nothing is not SqliteStorage._on_conflict_nothing
    # Confirms every other method (all six domain mixins + create_schema/
    # ensure_columns/migrate_api_budget_table) is inherited UNCHANGED, not
    # accidentally shadowed.
    assert PostgresStorage.get_interests is SqliteStorage.get_interests
    assert PostgresStorage.get_interest_push_state is SqliteStorage.get_interest_push_state
