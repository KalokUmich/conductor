"""Write-through chat persistence with micro-batch Postgres writes.

Every message is written to Redis (hot cache, 6h TTL) immediately by the
ConnectionManager.  This service additionally batches messages and writes
them to Postgres in groups of ``BATCH_SIZE`` (default 3).  A flush timer
ensures stragglers are written even in low-traffic rooms.

Postgres is always the **source of truth**.  Redis is a read cache.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Dict, List, Optional

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import sessionmaker

logger = logging.getLogger(__name__)

BATCH_SIZE = 3
FLUSH_DELAY = 5.0  # seconds


class ChatPersistenceService:
    """Manages write-through micro-batch persistence to Postgres."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session_factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        self._buffers: Dict[str, List[dict]] = {}  # room_id → pending messages
        self._flush_handles: Dict[str, asyncio.TimerHandle] = {}
        self._lock = asyncio.Lock()
        self._background_tasks: set = set()  # prevent GC of fire-and-forget tasks

    # ------------------------------------------------------------------
    # Write path: micro-batch
    # ------------------------------------------------------------------

    async def enqueue_message(self, room_id: str, message: dict) -> None:
        """Buffer a message and flush when batch is full."""
        async with self._lock:
            buf = self._buffers.setdefault(room_id, [])
            buf.append(message)

            if len(buf) >= BATCH_SIZE:
                batch = buf[:BATCH_SIZE]
                self._buffers[room_id] = buf[BATCH_SIZE:]
                self._cancel_timer(room_id)
                # Fire and forget — don't block the WebSocket handler
                task = asyncio.create_task(self._write_batch(room_id, batch))
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
            else:
                self._schedule_timer(room_id)

    def _schedule_timer(self, room_id: str) -> None:
        """Schedule a flush timer for a room (must hold _lock)."""
        if room_id in self._flush_handles:
            return  # already scheduled
        loop = asyncio.get_running_loop()
        handle = loop.call_later(
            FLUSH_DELAY,
            lambda rid=room_id: asyncio.create_task(self._flush_buffer(rid)),
        )
        self._flush_handles[room_id] = handle

    def _cancel_timer(self, room_id: str) -> None:
        """Cancel the flush timer for a room (must hold _lock)."""
        handle = self._flush_handles.pop(room_id, None)
        if handle:
            handle.cancel()

    async def _flush_buffer(self, room_id: str) -> None:
        """Flush any remaining messages in the buffer for *room_id*."""
        async with self._lock:
            self._cancel_timer(room_id)
            batch = self._buffers.pop(room_id, [])
        if batch:
            await self._write_batch(room_id, batch)

    async def _write_batch(self, room_id: str, messages: List[dict]) -> None:
        """INSERT a batch of messages (idempotent — skips existing IDs)."""
        if not messages:
            return
        try:
            from ..db.models import ChatMessageRecord

            async with self._session_factory() as session:
                for m in messages:
                    existing = await session.get(ChatMessageRecord, m["id"])
                    if existing:
                        continue  # idempotent: skip duplicates
                    # Build metadata JSON for structured message types
                    metadata_dict = m.get("metadata")
                    metadata_json = json.dumps(metadata_dict) if metadata_dict else None
                    session.add(
                        ChatMessageRecord(
                            id=m["id"],
                            room_id=room_id,
                            user_id=m.get("userId", ""),
                            display_name=m.get("displayName", ""),
                            role=m.get("role", "guest"),
                            type=m.get("type", "message"),
                            content=m.get("content", ""),
                            identity_source=m.get("identitySource", "anonymous"),
                            parent_message_id=m.get("parentMessageId"),
                            ai_data=json.dumps(m["aiData"]) if m.get("aiData") else None,
                            extra_data=metadata_json,
                            ts=m.get("ts", 0.0),
                        )
                    )
                await session.commit()
            logger.debug("Persisted %d messages for room %s", len(messages), room_id)
        except Exception as exc:
            logger.warning("Postgres batch write failed for room %s: %s", room_id, exc)

    # ------------------------------------------------------------------
    # Room lifecycle
    # ------------------------------------------------------------------

    async def ensure_room(
        self,
        room_id: str,
        owner_email: Optional[str] = None,
        owner_provider: Optional[str] = None,
        display_name: Optional[str] = None,
        name: Optional[str] = None,
        mode: str = "local",
        workspace_path: Optional[str] = None,
        repo_url: Optional[str] = None,
        branch: Optional[str] = None,
    ) -> None:
        """Create or touch a chat_rooms row (upsert).

        Uses merge() for cross-database compatibility (Postgres + SQLite).
        """
        try:
            from ..db.models import ChatRoom

            async with self._session_factory() as session:
                existing = await session.get(ChatRoom, room_id)
                if existing:
                    existing.last_active_at = datetime.now(UTC)
                    existing.status = "active"
                    # Update mutable fields if provided
                    if name is not None:
                        existing.name = name
                    if workspace_path is not None:
                        existing.workspace_path = workspace_path
                    if repo_url is not None:
                        existing.repo_url = repo_url
                    if branch is not None:
                        existing.branch = branch
                else:
                    session.add(
                        ChatRoom(
                            id=room_id,
                            name=name,
                            owner_email=owner_email,
                            owner_provider=owner_provider,
                            display_name=display_name,
                            mode=mode,
                            workspace_path=workspace_path,
                            repo_url=repo_url,
                            branch=branch,
                            status="active",
                        )
                    )
                await session.commit()
        except Exception as exc:
            logger.warning("ensure_room failed for %s: %s", room_id, exc)

    async def set_room_name(self, room_id: str, name: str) -> None:
        """Set or update the human-readable room name."""
        try:
            from ..db.models import ChatRoom

            async with self._session_factory() as session:
                room = await session.get(ChatRoom, room_id)
                if room:
                    room.name = name
                    await session.commit()
        except Exception as exc:
            logger.warning("set_room_name failed for %s: %s", room_id, exc)

    async def upsert_participant(
        self,
        room_id: str,
        user_id: str,
        display_name: str = "",
        role: str = "guest",
        identity_source: str = "anonymous",
        email: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> None:
        """Track a participant joining/rejoining a room."""
        try:
            from ..db.models import ChatRoomParticipant

            async with self._session_factory() as session:
                from sqlalchemy import and_

                q = select(ChatRoomParticipant).where(
                    and_(
                        ChatRoomParticipant.room_id == room_id,
                        ChatRoomParticipant.user_id == user_id,
                    )
                )
                result = await session.execute(q)
                existing = result.scalar_one_or_none()
                if existing:
                    existing.display_name = display_name
                    existing.role = role
                    existing.identity_source = identity_source
                    existing.is_active = True
                    existing.left_at = None
                    if email:
                        existing.email = email
                    if provider:
                        existing.provider = provider
                else:
                    session.add(
                        ChatRoomParticipant(
                            room_id=room_id,
                            user_id=user_id,
                            display_name=display_name,
                            role=role,
                            identity_source=identity_source,
                            email=email,
                            provider=provider,
                        )
                    )
                await session.commit()
        except Exception as exc:
            logger.warning("upsert_participant failed for %s/%s: %s", room_id, user_id, exc)

    async def mark_participant_left(self, room_id: str, user_id: str) -> None:
        """Mark a participant as having left the room."""
        try:
            from ..db.models import ChatRoomParticipant

            async with self._session_factory() as session:
                from sqlalchemy import and_

                q = select(ChatRoomParticipant).where(
                    and_(
                        ChatRoomParticipant.room_id == room_id,
                        ChatRoomParticipant.user_id == user_id,
                    )
                )
                result = await session.execute(q)
                p = result.scalar_one_or_none()
                if p:
                    p.is_active = False
                    p.left_at = datetime.now(UTC)
                    await session.commit()
        except Exception as exc:
            logger.warning("mark_participant_left failed for %s/%s: %s", room_id, user_id, exc)

    async def end_room(self, room_id: str) -> None:
        """Flush buffer, mark room as ended.  Redis is cleared by the caller."""
        await self._flush_buffer(room_id)
        try:
            from ..db.models import ChatRoom

            async with self._session_factory() as session:
                stmt = update(ChatRoom).where(ChatRoom.id == room_id).values(status="ended", ended_at=datetime.now(UTC))
                await session.execute(stmt)
                await session.commit()
        except Exception as exc:
            logger.warning("end_room failed for %s: %s", room_id, exc)

    async def delete_room(self, room_id: str) -> None:
        """Hard-delete room and all messages (CASCADE)."""
        await self._flush_buffer(room_id)
        try:
            from ..db.models import ChatRoom

            async with self._session_factory() as session:
                await session.execute(delete(ChatRoom).where(ChatRoom.id == room_id))
                await session.commit()
        except Exception as exc:
            logger.warning("delete_room failed for %s: %s", room_id, exc)

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    async def load_messages_from_postgres(
        self,
        room_id: str,
        since_ts: Optional[float] = None,
        limit: int = 500,
    ) -> List[dict]:
        """Load messages from Postgres, optionally filtered by timestamp."""
        try:
            from ..db.models import ChatMessageRecord

            async with self._session_factory() as session:
                q = select(ChatMessageRecord).where(ChatMessageRecord.room_id == room_id)
                if since_ts is not None:
                    q = q.where(ChatMessageRecord.ts > since_ts)
                q = q.order_by(ChatMessageRecord.ts).limit(limit)
                result = await session.execute(q)
                rows = result.scalars().all()
                return [
                    {
                        "id": r.id,
                        "roomId": room_id,
                        "userId": r.user_id,
                        "displayName": r.display_name,
                        "role": r.role,
                        "type": r.type,
                        "content": r.content,
                        "identitySource": r.identity_source,
                        "parentMessageId": r.parent_message_id,
                        "aiData": json.loads(r.ai_data) if r.ai_data else None,
                        "metadata": json.loads(r.extra_data) if r.extra_data else None,
                        "ts": r.ts,
                    }
                    for r in rows
                ]
        except Exception as exc:
            logger.warning("load_messages_from_postgres failed for %s: %s", room_id, exc)
            return []

    async def hydrate_room(self, room_id: str, redis_store=None) -> List[dict]:
        """Load messages: Redis first, Postgres fallback, write-back to Redis."""
        # Try Redis
        if redis_store:
            try:
                redis_msgs = await redis_store.get_messages(room_id)
                if redis_msgs:
                    logger.info("Hydrated room %s from Redis (%d messages)", room_id, len(redis_msgs))
                    return redis_msgs
            except Exception as exc:
                logger.warning("Redis hydrate failed for %s: %s", room_id, exc)

        # Fall back to Postgres
        pg_msgs = await self.load_messages_from_postgres(room_id)
        if not pg_msgs:
            return []

        # Write back to Redis for next access
        if redis_store:
            try:
                for msg in pg_msgs:
                    await redis_store.append_message(room_id, msg)
                logger.info("Wrote %d messages back to Redis for room %s", len(pg_msgs), room_id)
            except Exception as exc:
                logger.warning("Redis write-back failed for %s: %s", room_id, exc)

        return pg_msgs

    async def get_rooms_for_user(self, owner_email: str) -> List[dict]:
        """List active/ended rooms for an SSO user."""
        try:
            from ..db.models import ChatRoom

            async with self._session_factory() as session:
                q = (
                    select(ChatRoom)
                    .where(ChatRoom.owner_email == owner_email)
                    .where(ChatRoom.status.in_(["active", "ended"]))
                    .order_by(ChatRoom.last_active_at.desc())
                    .limit(20)
                )
                result = await session.execute(q)
                rows = result.scalars().all()
                return [
                    {
                        "id": r.id,
                        "name": r.name,
                        "display_name": r.display_name,
                        "mode": r.mode,
                        "workspace_path": r.workspace_path,
                        "repo_url": r.repo_url,
                        "branch": r.branch,
                        "status": r.status,
                        "created_at": r.created_at.isoformat() if r.created_at else None,
                        "last_active_at": r.last_active_at.isoformat() if r.last_active_at else None,
                    }
                    for r in rows
                ]
        except Exception as exc:
            logger.warning("get_rooms_for_user failed for %s: %s", owner_email, exc)
            return []

    async def get_messages_since(
        self,
        room_id: str,
        since_ts: float,
        limit: int = 500,
    ) -> List[dict]:
        """Incremental sync: messages newer than *since_ts*."""
        return await self.load_messages_from_postgres(room_id, since_ts=since_ts, limit=limit)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def flush_all(self) -> None:
        """Drain all room buffers.  Called on backend shutdown."""
        async with self._lock:
            room_ids = list(self._buffers.keys())
            for rid in room_ids:
                self._cancel_timer(rid)
        for rid in room_ids:
            await self._flush_buffer(rid)
        logger.info("ChatPersistenceService: flushed all buffers (%d rooms)", len(room_ids))
