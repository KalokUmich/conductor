"""In-memory credential store with TTL-based auto-expiry.

Credentials are NEVER written to disk.  The store lives for the process
lifetime only.  A background task sweeps expired entries.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from .schemas import CredentialPayload

logger = logging.getLogger(__name__)


@dataclass
class _StoredCredential:
    payload:    CredentialPayload
    stored_at:  float = field(default_factory=time.monotonic)
    expires_at: float = 0.0   # absolute monotonic deadline

    def is_expired(self) -> bool:
        return time.monotonic() >= self.expires_at


class CredentialStore:
    """Thread-safe (asyncio) in-memory store for PATs keyed by room_id."""

    def __init__(self, default_ttl_seconds: int = 3600) -> None:
        self._store: Dict[str, _StoredCredential] = {}
        self._lock  = asyncio.Lock()
        self._default_ttl = default_ttl_seconds
        self._sweep_task: Optional[asyncio.Task] = None  # type: ignore[type-arg]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start background sweep task."""
        self._sweep_task = asyncio.create_task(self._sweep_loop())
        logger.info("CredentialStore sweep task started (TTL=%ss)", self._default_ttl)

    async def stop(self) -> None:
        """Cancel sweep task and wipe all credentials."""
        if self._sweep_task:
            self._sweep_task.cancel()
            try:
                await self._sweep_task
            except asyncio.CancelledError:
                pass
        async with self._lock:
            self._store.clear()
        logger.info("CredentialStore stopped; all credentials wiped.")

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def put(
        self,
        room_id: str,
        payload: CredentialPayload,
        ttl_seconds: Optional[int] = None,
    ) -> None:
        """Store or replace credentials for *room_id*."""
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        entry = _StoredCredential(
            payload=payload,
            expires_at=time.monotonic() + ttl,
        )
        async with self._lock:
            self._store[room_id] = entry
        logger.debug("Credentials stored for room %s (TTL=%ss)", room_id, ttl)

    async def get(self, room_id: str) -> Optional[CredentialPayload]:
        """Return credentials if present and not expired, else *None*."""
        async with self._lock:
            entry = self._store.get(room_id)
        if entry is None:
            return None
        if entry.is_expired():
            await self.delete(room_id)
            logger.warning("Credentials for room %s expired", room_id)
            return None
        return entry.payload

    async def delete(self, room_id: str) -> None:
        """Remove credentials for *room_id* (no-op if absent)."""
        async with self._lock:
            self._store.pop(room_id, None)
        logger.debug("Credentials removed for room %s", room_id)

    async def has(self, room_id: str) -> bool:
        """Return True if unexpired credentials exist for *room_id*."""
        return (await self.get(room_id)) is not None

    # ------------------------------------------------------------------
    # Background sweep
    # ------------------------------------------------------------------

    async def _sweep_loop(self) -> None:
        sweep_interval = max(60, self._default_ttl // 4)
        while True:
            await asyncio.sleep(sweep_interval)
            await self._sweep_once()

    async def _sweep_once(self) -> None:
        """Evict all expired entries."""
        now = time.monotonic()
        async with self._lock:
            expired = [k for k, v in self._store.items() if now >= v.expires_at]
            for k in expired:
                del self._store[k]
        if expired:
            logger.info("CredentialStore sweep: evicted %d expired entries", len(expired))
