"""Async PostgreSQL-based audit log storage service.

Provides persistent storage for audit log entries using async SQLAlchemy.
The service accepts an ``AsyncEngine`` at construction time.
"""

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import List, Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from ..db.models import AuditLog
from .schemas import ApplyMode, AuditLogCreate, AuditLogEntry

logger = logging.getLogger(__name__)


class AuditLogService:
    """Service for managing audit logs in PostgreSQL."""

    _instance: Optional["AuditLogService"] = None

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session_factory = async_sessionmaker(engine, expire_on_commit=False)

    @classmethod
    def get_instance(cls, engine: Optional[AsyncEngine] = None) -> "AuditLogService":
        if cls._instance is None:
            if engine is None:
                raise RuntimeError("AuditLogService requires an AsyncEngine on first call")
            cls._instance = cls(engine)
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        cls._instance = None

    async def log_apply(self, entry: AuditLogCreate) -> AuditLogEntry:
        """Log an apply operation."""
        timestamp = datetime.now(UTC)
        row = AuditLog(
            room_id=entry.room_id,
            summary_id=entry.summary_id,
            changeset_hash=entry.changeset_hash,
            applied_by=entry.applied_by,
            mode=entry.mode.value,
            timestamp=timestamp,
        )
        async with self._session_factory() as session:
            session.add(row)
            await session.commit()

        return AuditLogEntry(
            room_id=entry.room_id,
            summary_id=entry.summary_id,
            changeset_hash=entry.changeset_hash,
            applied_by=entry.applied_by,
            mode=entry.mode,
            timestamp=timestamp,
        )

    async def get_logs(
        self,
        room_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[AuditLogEntry]:
        """Get audit logs, optionally filtered by room_id."""
        async with self._session_factory() as session:
            stmt = select(AuditLog).order_by(AuditLog.timestamp.desc()).limit(limit)
            if room_id:
                stmt = stmt.where(AuditLog.room_id == room_id)
            result = await session.execute(stmt)
            return [
                AuditLogEntry(
                    room_id=row.room_id,
                    summary_id=row.summary_id,
                    changeset_hash=row.changeset_hash,
                    applied_by=row.applied_by,
                    mode=ApplyMode(row.mode),
                    timestamp=row.timestamp,
                )
                for row in result.scalars().all()
            ]

    async def delete_room_logs(self, room_id: str) -> None:
        """Delete all audit entries for a room."""
        async with self._session_factory() as session:
            await session.execute(delete(AuditLog).where(AuditLog.room_id == room_id))
            await session.commit()
            logger.info("[Audit] Deleted audit logs for room %s", room_id)


def compute_changeset_hash(changeset: dict) -> str:
    """Compute a SHA-256 hash (truncated) for a changeset."""
    changeset_str = json.dumps(changeset, sort_keys=True)
    return hashlib.sha256(changeset_str.encode()).hexdigest()[:16]
