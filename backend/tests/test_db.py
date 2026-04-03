"""Tests for the database abstraction layer (engine, models, redis)."""

from datetime import UTC

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import (
    AuditLog,
    IntegrationToken,
    Todo,
)


@pytest.mark.asyncio
async def test_engine_creates_all_tables(db_engine):
    """All ORM models are created in the in-memory database."""
    from sqlalchemy import inspect

    async with db_engine.connect() as conn:
        table_names = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())

    expected = {
        "repo_tokens",
        "session_traces",
        "audit_logs",
        "file_metadata",
        "todos",
        "integration_tokens",
    }
    assert expected.issubset(set(table_names)), f"Missing tables: {expected - set(table_names)}"


@pytest.mark.asyncio
async def test_todo_crud(db_engine):
    """Basic CRUD on the Todo model."""
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with session_factory() as session:
        todo = Todo(
            id="test-1",
            room_id="room-1",
            title="Fix bug",
            type="task",
            priority="high",
            status="open",
            created_by="user-1",
            source="manual",
        )
        session.add(todo)
        await session.commit()

    async with session_factory() as session:
        result = await session.execute(select(Todo).where(Todo.id == "test-1"))
        row = result.scalar_one_or_none()
        assert row is not None
        assert row.title == "Fix bug"
        assert row.priority == "high"


@pytest.mark.asyncio
async def test_audit_log_insert(db_engine):
    """Insert and query an audit log entry."""
    from datetime import datetime

    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with session_factory() as session:
        entry = AuditLog(
            room_id="room-1",
            changeset_hash="abc123",
            applied_by="user-1",
            mode="manual",
            timestamp=datetime.now(UTC),
        )
        session.add(entry)
        await session.commit()

    async with session_factory() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.room_id == "room-1"))
        rows = result.scalars().all()
        assert len(rows) == 1
        assert rows[0].changeset_hash == "abc123"


@pytest.mark.asyncio
async def test_integration_token_unique_constraint(db_engine):
    """The (user_email, provider) unique constraint is enforced."""
    from sqlalchemy.exc import IntegrityError

    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with session_factory() as session:
        t1 = IntegrationToken(
            user_email="alice@example.com",
            provider="jira",
            access_token="token-1",
        )
        session.add(t1)
        await session.commit()

    with pytest.raises(IntegrityError):
        async with session_factory() as session:
            t2 = IntegrationToken(
                user_email="alice@example.com",
                provider="jira",
                access_token="token-2",
            )
            session.add(t2)
            await session.commit()


@pytest.mark.asyncio
async def test_redis_store_basic(redis_mock):
    """Basic operations on RedisChatStore."""
    if redis_mock is None:
        pytest.skip("fakeredis not installed")

    from app.chat.redis_store import RedisChatStore

    store = RedisChatStore(redis_mock)

    # Messages
    await store.append_message("room-1", {"content": "hello", "id": "m1"})
    messages = await store.get_messages("room-1")
    assert len(messages) == 1
    assert messages[0]["content"] == "hello"

    # Dedup
    is_dup = await store.is_duplicate("room-1", "m1")
    assert is_dup is False  # first time
    is_dup2 = await store.is_duplicate("room-1", "m1")
    assert is_dup2 is True  # second time

    # Host/Lead
    await store.set_host("room-1", "user-1")
    assert await store.get_host("room-1") == "user-1"

    await store.set_lead("room-1", "user-2")
    assert await store.get_lead("room-1") == "user-2"

    # Clear room
    await store.clear_room("room-1")
    assert await store.get_host("room-1") is None
    msgs = await store.get_messages("room-1")
    assert len(msgs) == 0
