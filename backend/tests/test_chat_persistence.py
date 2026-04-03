"""Tests for chat persistence service (write-through micro-batch)."""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.chat.manager import (
    _active_blockers,
    check_end_chat_blockers,
    register_blocker,
    unregister_blocker,
)
from app.chat.persistence import BATCH_SIZE, ChatPersistenceService
from app.db.models import Base

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_engine():
    """In-memory async SQLite engine for tests."""
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        # Enable foreign key support in SQLite (required for CASCADE)
        await conn.execute(text("PRAGMA foreign_keys = ON"))
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def persistence(db_engine):
    """ChatPersistenceService backed by in-memory SQLite."""
    svc = ChatPersistenceService(db_engine)
    yield svc
    await svc.flush_all()


def _msg(room_id: str, idx: int) -> dict:
    """Helper: build a chat message dict."""
    return {
        "id": f"msg-{room_id}-{idx}",
        "roomId": room_id,
        "userId": "user-1",
        "displayName": "Alice",
        "role": "host",
        "type": "message",
        "content": f"Hello {idx}",
        "aiData": None,
        "ts": 1000.0 + idx,
    }


# ---------------------------------------------------------------------------
# Micro-batch tests
# ---------------------------------------------------------------------------


class TestMicroBatch:
    """Verify micro-batch buffering and flush behavior."""

    @pytest.mark.asyncio
    async def test_batch_triggers_at_threshold(self, persistence):
        """3 messages should trigger a batch INSERT."""
        room = "room-batch"
        await persistence.ensure_room(room)

        for i in range(BATCH_SIZE):
            await persistence.enqueue_message(room, _msg(room, i))

        # Give the fire-and-forget task a moment to complete
        await asyncio.sleep(0.1)

        msgs = await persistence.load_messages_from_postgres(room)
        assert len(msgs) == BATCH_SIZE

    @pytest.mark.asyncio
    async def test_partial_batch_stays_in_buffer(self, persistence):
        """< 3 messages should stay in the buffer, not in Postgres yet."""
        room = "room-partial"
        await persistence.ensure_room(room)

        await persistence.enqueue_message(room, _msg(room, 0))
        await persistence.enqueue_message(room, _msg(room, 1))

        # Not enough for a batch — should be buffered
        msgs = await persistence.load_messages_from_postgres(room)
        assert len(msgs) == 0

    @pytest.mark.asyncio
    async def test_flush_buffer_drains_partial(self, persistence):
        """Explicit flush should write partial batch to Postgres."""
        room = "room-flush"
        await persistence.ensure_room(room)

        await persistence.enqueue_message(room, _msg(room, 0))
        await persistence._flush_buffer(room)

        msgs = await persistence.load_messages_from_postgres(room)
        assert len(msgs) == 1

    @pytest.mark.asyncio
    async def test_flush_all_drains_all_rooms(self, persistence):
        """flush_all should drain buffers for all rooms."""
        for r in ["room-a", "room-b"]:
            await persistence.ensure_room(r)
            await persistence.enqueue_message(r, _msg(r, 0))

        await persistence.flush_all()

        for r in ["room-a", "room-b"]:
            msgs = await persistence.load_messages_from_postgres(r)
            assert len(msgs) == 1

    @pytest.mark.asyncio
    async def test_idempotent_insert(self, persistence):
        """Duplicate message IDs should not cause errors (ON CONFLICT DO NOTHING)."""
        room = "room-dedup"
        await persistence.ensure_room(room)
        msg = _msg(room, 0)

        await persistence.enqueue_message(room, msg)
        await persistence._flush_buffer(room)
        # Insert same message again
        await persistence.enqueue_message(room, msg)
        await persistence._flush_buffer(room)

        msgs = await persistence.load_messages_from_postgres(room)
        assert len(msgs) == 1


# ---------------------------------------------------------------------------
# Room lifecycle tests
# ---------------------------------------------------------------------------


class TestRoomLifecycle:
    @pytest.mark.asyncio
    async def test_ensure_room_creates_row(self, persistence):
        await persistence.ensure_room("room-new", owner_email="a@b.com")
        rooms = await persistence.get_rooms_for_user("a@b.com")
        assert len(rooms) == 1
        assert rooms[0]["id"] == "room-new"

    @pytest.mark.asyncio
    async def test_end_room_marks_status(self, persistence):
        room = "room-end"
        await persistence.ensure_room(room, owner_email="a@b.com")
        await persistence.enqueue_message(room, _msg(room, 0))

        await persistence.end_room(room)

        rooms = await persistence.get_rooms_for_user("a@b.com")
        assert rooms[0]["status"] == "ended"
        # Messages should still be in Postgres (not deleted)
        msgs = await persistence.load_messages_from_postgres(room)
        assert len(msgs) == 1

    @pytest.mark.asyncio
    async def test_delete_room_cascades(self, persistence):
        room = "room-delete"
        await persistence.ensure_room(room, owner_email="a@b.com")
        await persistence.enqueue_message(room, _msg(room, 0))
        await persistence._flush_buffer(room)

        await persistence.delete_room(room)

        msgs = await persistence.load_messages_from_postgres(room)
        assert len(msgs) == 0
        rooms = await persistence.get_rooms_for_user("a@b.com")
        assert len(rooms) == 0


# ---------------------------------------------------------------------------
# Read path tests
# ---------------------------------------------------------------------------


class TestReadPath:
    @pytest.mark.asyncio
    async def test_load_messages_since(self, persistence):
        room = "room-since"
        await persistence.ensure_room(room)
        for i in range(5):
            await persistence.enqueue_message(room, _msg(room, i))
        await persistence._flush_buffer(room)

        # Only messages after ts=1002 (i.e., idx 3 and 4)
        msgs = await persistence.get_messages_since(room, since_ts=1002.0)
        assert len(msgs) == 2
        assert msgs[0]["ts"] == 1003.0

    @pytest.mark.asyncio
    async def test_hydrate_room_from_postgres(self, persistence):
        """hydrate_room should load from Postgres when Redis is unavailable."""
        room = "room-hydrate"
        await persistence.ensure_room(room)
        for i in range(3):
            await persistence.enqueue_message(room, _msg(room, i))
        # The 3rd message triggers a fire-and-forget batch write;
        # wait for it to complete before querying.
        await asyncio.sleep(0.2)
        await persistence._flush_buffer(room)

        msgs = await persistence.hydrate_room(room, redis_store=None)
        assert len(msgs) == 3


# ---------------------------------------------------------------------------
# End Chat Blocker System tests
# ---------------------------------------------------------------------------


class TestEndChatBlockers:
    def setup_method(self):
        """Clean global state before each test."""
        _active_blockers.clear()

    def test_no_blockers_returns_empty(self):
        assert check_end_chat_blockers("room-1") == []

    def test_register_and_check(self):
        register_blocker("room-1", "agent_running")
        assert check_end_chat_blockers("room-1") == ["agent_running"]

    def test_unregister_clears_blocker(self):
        register_blocker("room-1", "agent_running")
        unregister_blocker("room-1", "agent_running")
        assert check_end_chat_blockers("room-1") == []

    def test_multiple_blockers(self):
        register_blocker("room-1", "agent_running")
        register_blocker("room-1", "file_upload")
        blockers = check_end_chat_blockers("room-1")
        assert set(blockers) == {"agent_running", "file_upload"}

    def test_different_rooms_isolated(self):
        register_blocker("room-1", "agent_running")
        assert check_end_chat_blockers("room-2") == []

    def test_unregister_nonexistent_safe(self):
        unregister_blocker("room-1", "nonexistent")
        assert check_end_chat_blockers("room-1") == []
