from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from dotenv import load_dotenv
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlmodel import SQLModel

from alembic import context

# Load .env from project root so DATABASE_URL is available
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# Import models so SQLModel.metadata is populated
import src.app.db.models  # noqa: F401,E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _to_asyncpg_url(raw: str) -> str:
    if raw.startswith("postgresql+asyncpg://"):
        url = raw
    elif raw.startswith("postgresql://"):
        url = raw.replace("postgresql://", "postgresql+asyncpg://", 1)
    elif raw.startswith("postgres://"):
        url = raw.replace("postgres://", "postgresql+asyncpg://", 1)
    else:
        raise ValueError(f"Unsupported DATABASE_URL scheme: {raw.split('://', 1)[0]}")

    parts = urlsplit(url)
    filtered = [
        (k, v) for k, v in parse_qsl(parts.query)
        if k not in {"sslmode", "channel_binding"}
    ]
    return urlunsplit(parts._replace(query=urlencode(filtered)))


database_url = os.environ.get("DATABASE_URL")
if database_url:
    config.set_main_option("sqlalchemy.url", _to_asyncpg_url(database_url))


target_metadata = SQLModel.metadata


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
