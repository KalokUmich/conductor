"""Async SQLAlchemy engine and session management.

Resolution order for database URL:
  1. Environment variable ``DATABASE_URL``
  2. YAML config ``postgres.url``
  3. Default: ``postgresql+asyncpg://conductor:conductor@localhost:5432/conductor``
"""
from __future__ import annotations

import logging
import os
from typing import AsyncGenerator, Optional

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .models import Base

logger = logging.getLogger(__name__)

_DEFAULT_URL = "postgresql+asyncpg://conductor:conductor@localhost:5432/conductor"

_engine: Optional[AsyncEngine] = None
_session_factory: Optional[async_sessionmaker[AsyncSession]] = None


def get_engine() -> AsyncEngine:
    """Return the process-wide async engine (must call ``init_db`` first)."""
    if _engine is None:
        raise RuntimeError("Database not initialised — call init_db() first")
    return _engine


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield an ``AsyncSession`` for use in a request scope."""
    if _session_factory is None:
        raise RuntimeError("Database not initialised — call init_db() first")
    async with _session_factory() as session:
        yield session


async def init_db(
    url: Optional[str] = None,
    pool_size: int = 10,
    max_overflow: int = 20,
    echo: bool = False,
) -> AsyncEngine:
    """Create the async engine and (optionally) create all tables.

    Args:
        url: Database URL. Falls back to ``DATABASE_URL`` env var, then default.
        pool_size: Connection pool size.
        max_overflow: Max overflow connections above pool_size.
        echo: Echo SQL statements for debugging.

    Returns:
        The created ``AsyncEngine``.
    """
    global _engine, _session_factory

    resolved_url = os.environ.get("DATABASE_URL") or url or _DEFAULT_URL
    logger.info("Initialising database: %s", _mask_url(resolved_url))

    _engine = create_async_engine(
        resolved_url,
        pool_size=pool_size,
        max_overflow=max_overflow,
        echo=echo,
        pool_pre_ping=True,
    )
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)

    # Schema is managed by Liquibase (database/changelog/).
    # Run `make db-update` before starting the backend.

    logger.info("Database initialised successfully")
    return _engine


async def close_db() -> None:
    """Dispose of the engine and release all pooled connections."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        logger.info("Database connection pool closed")
        _engine = None
        _session_factory = None


def _mask_url(url: str) -> str:
    """Mask password in URL for safe logging."""
    if "@" in url and "://" in url:
        scheme_rest = url.split("://", 1)
        if len(scheme_rest) == 2:
            creds_host = scheme_rest[1].split("@", 1)
            if len(creds_host) == 2:
                return f"{scheme_rest[0]}://***@{creds_host[1]}"
    return url
