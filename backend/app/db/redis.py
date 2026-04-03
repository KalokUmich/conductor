"""Redis connection management for Conductor.

Resolution order for Redis URL:
  1. Environment variable ``REDIS_URL``
  2. YAML config ``redis.url``
  3. Default: ``redis://localhost:6379/0``
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DEFAULT_URL = "redis://localhost:6379/0"

_redis = None


def get_redis() -> Optional[Any]:
    """Return the process-wide Redis client (must call ``init_redis`` first).

    Returns ``None`` if Redis is not configured or unavailable.
    """
    return _redis


async def init_redis(url: Optional[str] = None, prefix: str = "conductor:") -> Optional[Any]:
    """Create the Redis connection pool.

    Args:
        url: Redis URL. Falls back to ``REDIS_URL`` env var, then default.
        prefix: Key prefix for all Conductor keys.

    Returns:
        The Redis client, or None if connection fails.
    """
    global _redis
    resolved_url = os.environ.get("REDIS_URL") or url or _DEFAULT_URL

    try:
        import redis.asyncio as aioredis

        _redis = aioredis.from_url(
            resolved_url,
            decode_responses=True,
            max_connections=20,
        )
        # Test connectivity
        await _redis.ping()
        logger.info("Redis connected: %s", _mask_url(resolved_url))
        return _redis
    except Exception as exc:
        logger.warning("Redis unavailable (%s) — falling back to in-memory", exc)
        _redis = None
        return None


async def close_redis() -> None:
    """Close the Redis connection pool."""
    global _redis
    if _redis is not None:
        await _redis.aclose()
        logger.info("Redis connection closed")
        _redis = None


def _mask_url(url: str) -> str:
    """Mask password in URL for safe logging."""
    if "@" in url:
        parts = url.split("@", 1)
        return f"***@{parts[1]}"
    return url
