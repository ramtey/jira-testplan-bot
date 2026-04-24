from __future__ import annotations

import os
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession


def _build_async_url(raw: str) -> str:
    if raw.startswith("postgresql+asyncpg://"):
        return raw
    if raw.startswith("postgresql://"):
        return raw.replace("postgresql://", "postgresql+asyncpg://", 1)
    if raw.startswith("postgres://"):
        return raw.replace("postgres://", "postgresql+asyncpg://", 1)
    raise ValueError(f"Unsupported DATABASE_URL scheme: {raw.split('://', 1)[0]}")


def _strip_asyncpg_incompatible_params(url: str) -> str:
    # asyncpg ignores sslmode/channel_binding from the URL; it uses TLS by default
    # against Neon. Leaving them in a URL is fine for libpq but breaks asyncpg parsing.
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

    parts = urlsplit(url)
    filtered = [(k, v) for k, v in parse_qsl(parts.query) if k not in {"sslmode", "channel_binding"}]
    return urlunsplit(parts._replace(query=urlencode(filtered)))


engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _resolve_database_url(override: str | None = None) -> str:
    if override:
        return override
    env_val = os.environ.get("DATABASE_URL")
    if env_val:
        return env_val
    # Fall back to pydantic-settings (auto-loads .env) so imports that don't read
    # env directly still work. Imported lazily to avoid a module-load cycle.
    from src.app.config import settings
    if settings.database_url:
        return settings.database_url
    raise RuntimeError(
        "DATABASE_URL is not set — configure it in .env or pass it to init_engine()"
    )


def init_engine(database_url: str | None = None) -> AsyncEngine:
    global engine, _sessionmaker
    raw = _resolve_database_url(database_url)
    url = _strip_asyncpg_incompatible_params(_build_async_url(raw))
    engine = create_async_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=10)
    _sessionmaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        init_engine()
    assert _sessionmaker is not None
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        yield session
