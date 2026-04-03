"""Persistent repo-scoped token cache backed by PostgreSQL (async SQLAlchemy).

Stores proven-valid PATs keyed by normalised repo URL so that multiple
chat rooms cloning the same repo can reuse a non-expired token without
the user having to supply it again.

Security notes
--------------
* Tokens are stored in plaintext.  Treat the database with the same care as
  a ``.env`` file or SSH private key.
* The cache is opportunistic: failures to read / write are logged and
  swallowed so they never break workspace creation.

Expiry policy
-------------
When the caller provides ``CredentialPayload.expires_at``, that
timestamp is used as-is.  If the field is ``None`` (e.g. a classic
GitHub PAT with no expiry date), a configurable default TTL (default
8 hours) is applied from the moment of caching.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from ..db.models import RepoToken
from .schemas import CredentialPayload

logger = logging.getLogger(__name__)

_DEFAULT_TTL_SECONDS = 8 * 3600  # 8 hours


def _normalize_url(url: str) -> str:
    """Return a canonical form of *url* used as the cache key."""
    return url.strip().rstrip("/").removesuffix(".git")


class RepoTokenCache:
    """Async PostgreSQL-backed cache of PATs keyed by (normalised) repo URL."""

    def __init__(
        self,
        engine: AsyncEngine,
        default_ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    ) -> None:
        self._engine = engine
        self._default_ttl = default_ttl_seconds
        self._session_factory = async_sessionmaker(engine, expire_on_commit=False)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def put(self, repo_url: str, creds: CredentialPayload) -> None:
        """Insert or replace a token for *repo_url*.

        Also evicts expired entries on every write (cheap housekeeping).
        """
        key = _normalize_url(repo_url)
        now = datetime.now(UTC)

        if creds.expires_at is not None:
            expires: datetime = creds.expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
        else:
            expires = now + timedelta(seconds=self._default_ttl)

        # Opportunistic cleanup
        await self.evict_expired()

        try:
            async with self._session_factory() as session:
                # Upsert: delete existing then insert (works with all backends)
                await session.execute(delete(RepoToken).where(RepoToken.repo_url == key))
                session.add(
                    RepoToken(
                        repo_url=key,
                        token=creds.token,
                        username=creds.username,
                        cached_at=now,
                        expires_at=expires,
                    )
                )
                await session.commit()
                logger.info(
                    "Cached token for repo %s (expires %s)",
                    key,
                    expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
                )
        except Exception as exc:
            logger.warning("Failed to cache token for %s: %s", key, exc)

    async def get(self, repo_url: str) -> Optional[CredentialPayload]:
        """Return a non-expired cached credential, or *None*."""
        key = _normalize_url(repo_url)
        now = datetime.now(UTC)

        try:
            async with self._session_factory() as session:
                result = await session.execute(select(RepoToken).where(RepoToken.repo_url == key))
                row = result.scalar_one_or_none()
        except Exception as exc:
            logger.warning("Token cache read failed for %s: %s", key, exc)
            return None

        if row is None:
            return None

        expires_at = row.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)

        if now >= expires_at:
            logger.info("Cached token for repo %s has expired; evicting", key)
            await self._evict_one(key)
            return None

        logger.info("Using cached token for repo %s", key)
        return CredentialPayload(
            token=row.token,
            username=row.username or None,
            expires_at=expires_at,
        )

    async def evict_expired(self) -> int:
        """Delete all expired rows.  Returns the number of rows removed."""
        now = datetime.now(UTC)
        try:
            async with self._session_factory() as session:
                result = await session.execute(delete(RepoToken).where(RepoToken.expires_at <= now))
                await session.commit()
                count = result.rowcount
                if count:
                    logger.info("RepoTokenCache: evicted %d expired token(s)", count)
                return count
        except Exception as exc:
            logger.warning("Token cache eviction failed: %s", exc)
            return 0

    async def list_entries(self) -> list[dict]:
        """Return all cached entries (tokens redacted) for diagnostics."""
        try:
            async with self._session_factory() as session:
                result = await session.execute(
                    select(
                        RepoToken.repo_url,
                        RepoToken.username,
                        RepoToken.cached_at,
                        RepoToken.expires_at,
                    )
                )
                return [
                    {
                        "repo_url": r.repo_url,
                        "username": r.username,
                        "cached_at": r.cached_at.isoformat() if r.cached_at else None,
                        "expires_at": r.expires_at.isoformat() if r.expires_at else None,
                    }
                    for r in result.all()
                ]
        except Exception as exc:
            logger.warning("Token cache list failed: %s", exc)
            return []

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _evict_one(self, repo_url: str) -> None:
        try:
            async with self._session_factory() as session:
                await session.execute(delete(RepoToken).where(RepoToken.repo_url == repo_url))
                await session.commit()
        except Exception as exc:
            logger.warning("Could not evict token for %s: %s", repo_url, exc)
