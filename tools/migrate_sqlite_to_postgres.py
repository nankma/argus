"""
One-off data migration: copies every row from a SQLite subscribers.db
into a Postgres database, using storage/schema.py's Table definitions as
the single source of truth for both target-schema creation and column
order -- so this script never hardcodes a column list that could drift
from the real schema.

Usage:
    python tools/migrate_sqlite_to_postgres.py <sqlite_path> <postgres_url>

Example:
    python tools/migrate_sqlite_to_postgres.py \
        subscribers_int_export.db \
        postgresql+psycopg2://myfirstagent:PASSWORD@192.168.0.26:5432/myfirstagent_int

Idempotent-ish: creates the target schema if missing (metadata.create_all,
checkfirst=True), then INSERTs every source row. Meant to run against an
EMPTY target database -- run once, not repeatedly, or duplicate rows will
land on tables without a natural unique constraint covering every column.
"""

import sys

from sqlalchemy import create_engine, insert, select

from storage import schema


def migrate(sqlite_path: str, postgres_url: str) -> None:
    source = create_engine(f"sqlite:///{sqlite_path}")
    target = create_engine(postgres_url)

    schema.metadata.create_all(target)

    with source.connect() as src_conn, target.begin() as dst_conn:
        for table in schema.metadata.sorted_tables:
            rows = src_conn.execute(select(table)).mappings().all()
            if not rows:
                print(f"{table.name}: 0 rows (skipped)")
                continue
            dst_conn.execute(insert(table), [dict(row) for row in rows])
            print(f"{table.name}: {len(rows)} row(s) copied")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    migrate(sys.argv[1], sys.argv[2])
