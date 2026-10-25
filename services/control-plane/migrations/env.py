"""Alembic environment. Migrations run as the schema-owner role (RE_DATABASE_MIGRATION_URL)."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool

from reliability.platform.settings import get_settings


def _sync_url(url: str) -> str:
    return url.replace("+asyncpg", "+psycopg")


def run_migrations_online() -> None:
    url = context.config.get_main_option("sqlalchemy.url") or get_settings().database_migration_url
    engine = create_engine(_sync_url(url), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise SystemExit("offline migrations are not supported")
run_migrations_online()
