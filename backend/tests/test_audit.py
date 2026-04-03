"""Unit tests for audit log functionality."""

from datetime import datetime

import pytest
import pytest_asyncio

from app.audit.schemas import ApplyMode, AuditLogCreate, AuditLogEntry
from app.audit.service import AuditLogService, compute_changeset_hash


class TestAuditLogEntry:
    """Tests for AuditLogEntry schema."""

    def test_audit_log_entry_creation(self):
        entry = AuditLogEntry(
            room_id="room-123",
            summary_id="summary-456",
            changeset_hash="abc123def456",
            applied_by="user@example.com",
            mode=ApplyMode.MANUAL,
        )
        assert entry.room_id == "room-123"
        assert entry.summary_id == "summary-456"
        assert entry.changeset_hash == "abc123def456"
        assert entry.applied_by == "user@example.com"
        assert entry.mode == ApplyMode.MANUAL
        assert isinstance(entry.timestamp, datetime)

    def test_audit_log_entry_with_optional_summary_id(self):
        entry = AuditLogEntry(
            room_id="room-123",
            changeset_hash="abc123",
            applied_by="user@example.com",
            mode=ApplyMode.AUTO,
        )
        assert entry.summary_id is None
        assert entry.mode == ApplyMode.AUTO

    def test_apply_mode_values(self):
        assert ApplyMode.MANUAL.value == "manual"
        assert ApplyMode.AUTO.value == "auto"


class TestAuditLogService:
    """Tests for AuditLogService with async SQLAlchemy."""

    @pytest_asyncio.fixture
    async def audit_service(self, db_engine):
        AuditLogService.reset_instance()
        service = AuditLogService(engine=db_engine)
        yield service
        AuditLogService.reset_instance()

    @pytest.mark.asyncio
    async def test_log_apply_creates_entry(self, audit_service):
        create_entry = AuditLogCreate(
            room_id="room-123",
            summary_id="summary-456",
            changeset_hash="hash123",
            applied_by="user@example.com",
            mode=ApplyMode.MANUAL,
        )
        result = await audit_service.log_apply(create_entry)
        assert result.room_id == "room-123"
        assert result.changeset_hash == "hash123"
        assert result.mode == ApplyMode.MANUAL

    @pytest.mark.asyncio
    async def test_log_apply_persists_entry(self, audit_service):
        create_entry = AuditLogCreate(
            room_id="room-789",
            changeset_hash="hash456",
            applied_by="admin@example.com",
            mode=ApplyMode.AUTO,
        )
        await audit_service.log_apply(create_entry)
        logs = await audit_service.get_logs(room_id="room-789")
        assert len(logs) == 1
        assert logs[0].changeset_hash == "hash456"

    @pytest.mark.asyncio
    async def test_get_logs_returns_all_logs(self, audit_service):
        for i in range(3):
            await audit_service.log_apply(
                AuditLogCreate(
                    room_id=f"room-{i}",
                    changeset_hash=f"hash-{i}",
                    applied_by="user@example.com",
                    mode=ApplyMode.MANUAL,
                )
            )
        logs = await audit_service.get_logs()
        assert len(logs) == 3

    @pytest.mark.asyncio
    async def test_get_logs_filters_by_room_id(self, audit_service):
        await audit_service.log_apply(
            AuditLogCreate(
                room_id="room-a",
                changeset_hash="hash-1",
                applied_by="user@example.com",
                mode=ApplyMode.MANUAL,
            )
        )
        await audit_service.log_apply(
            AuditLogCreate(
                room_id="room-b",
                changeset_hash="hash-2",
                applied_by="user@example.com",
                mode=ApplyMode.AUTO,
            )
        )
        logs_a = await audit_service.get_logs(room_id="room-a")
        logs_b = await audit_service.get_logs(room_id="room-b")
        assert len(logs_a) == 1
        assert logs_a[0].room_id == "room-a"
        assert len(logs_b) == 1
        assert logs_b[0].room_id == "room-b"

    @pytest.mark.asyncio
    async def test_delete_room_logs(self, audit_service):
        await audit_service.log_apply(
            AuditLogCreate(
                room_id="room-del",
                changeset_hash="hash-del",
                applied_by="user@example.com",
                mode=ApplyMode.MANUAL,
            )
        )
        await audit_service.delete_room_logs("room-del")
        logs = await audit_service.get_logs(room_id="room-del")
        assert len(logs) == 0


class TestChangesetHash:
    """Tests for changeset hash computation."""

    def test_compute_changeset_hash_returns_string(self):
        changeset = {"changes": [{"file": "test.py"}]}
        result = compute_changeset_hash(changeset)
        assert isinstance(result, str)
        assert len(result) == 16

    def test_compute_changeset_hash_is_deterministic(self):
        changeset = {"changes": [{"file": "test.py", "type": "create_file"}]}
        hash1 = compute_changeset_hash(changeset)
        hash2 = compute_changeset_hash(changeset)
        assert hash1 == hash2

    def test_compute_changeset_hash_different_for_different_input(self):
        changeset1 = {"changes": [{"file": "test1.py"}]}
        changeset2 = {"changes": [{"file": "test2.py"}]}
        hash1 = compute_changeset_hash(changeset1)
        hash2 = compute_changeset_hash(changeset2)
        assert hash1 != hash2
