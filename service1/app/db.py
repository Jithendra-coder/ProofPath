from pathlib import Path

import psycopg
from psycopg.rows import dict_row


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = PROJECT_ROOT / "service1" / "migrations"


def connect_database(database_url: str) -> psycopg.Connection:
    return psycopg.connect(database_url, row_factory=dict_row, connect_timeout=5)


def migrate_database(database_url: str) -> None:
    with connect_database(database_url) as connection:
        with connection.transaction():
            connection.execute("SELECT pg_advisory_xact_lock(781236540112)")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )"""
            )
            for migration in sorted(MIGRATIONS.glob("*.sql")):
                if connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = %s", (migration.name,)
                ).fetchone():
                    continue
                for statement in migration.read_text(encoding="utf-8").split(";"):
                    if statement.strip():
                        connection.execute(statement)
                connection.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (migration.name,))
