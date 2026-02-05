"""Unit tests for audit log functionality."""

import os
import tempfile
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.audit.schemas import ApplyMode, AuditLogCreate, AuditLogEntry
from app.audit.service import AuditLogService, compute_changeset_hash


client = TestClient(app)


@pytest.fixture
def temp_db():
    """Create a temporary database path for testing."""
    # Create a unique temp file path but don't create the file
    # DuckDB will create a valid database file
    db_path = tempfile.mktemp(suffix=".duckdb")

    yield db_path

    # Cleanup
    AuditLogService.reset_instance()
    if os.path.exists(db_path):
        os.remove(db_path)
    # Also clean up WAL file if exists
    wal_path = db_path + ".wal"
    if os.path.exists(wal_path):
        os.remove(wal_path)


@pytest.fixture
def audit_service(temp_db):
    """Create an audit log service with a temp database."""
    AuditLogService.reset_instance()
    service = AuditLogService(db_path=temp_db)
    yield service
    service.close()


class TestAuditLogEntry:
    """Tests for AuditLogEntry schema."""
    
    def test_audit_log_entry_creation(self):
        """Test creating an audit log entry."""
        entry = AuditLogEntry(
            room_id="room-123",
            summary_id="summary-456",
            changeset_hash="abc123def456",
            applied_by="user@example.com",
            mode=ApplyMode.MANUAL
        )
        
        assert entry.room_id == "room-123"
        assert entry.summary_id == "summary-456"
        assert entry.changeset_hash == "abc123def456"
        assert entry.applied_by == "user@example.com"
        assert entry.mode == ApplyMode.MANUAL
        assert isinstance(entry.timestamp, datetime)
    
    def test_audit_log_entry_with_optional_summary_id(self):
        """Test creating an entry without summary_id."""
        entry = AuditLogEntry(
            room_id="room-123",
            changeset_hash="abc123",
            applied_by="user@example.com",
            mode=ApplyMode.AUTO
        )
        
        assert entry.summary_id is None
        assert entry.mode == ApplyMode.AUTO
    
    def test_apply_mode_values(self):
        """Test ApplyMode enum values."""
        assert ApplyMode.MANUAL.value == "manual"
        assert ApplyMode.AUTO.value == "auto"


class TestAuditLogService:
    """Tests for AuditLogService."""
    
    def test_log_apply_creates_entry(self, audit_service):
        """Test that log_apply creates an audit log entry."""
        create_entry = AuditLogCreate(
            room_id="room-123",
            summary_id="summary-456",
            changeset_hash="hash123",
            applied_by="user@example.com",
            mode=ApplyMode.MANUAL
        )
        
        result = audit_service.log_apply(create_entry)
        
        assert result.room_id == "room-123"
        assert result.summary_id == "summary-456"
        assert result.changeset_hash == "hash123"
        assert result.applied_by == "user@example.com"
        assert result.mode == ApplyMode.MANUAL
        assert isinstance(result.timestamp, datetime)
    
    def test_log_apply_persists_entry(self, audit_service):
        """Test that logged entries are persisted."""
        create_entry = AuditLogCreate(
            room_id="room-789",
            changeset_hash="hash456",
            applied_by="admin@example.com",
            mode=ApplyMode.AUTO
        )
        
        audit_service.log_apply(create_entry)
        logs = audit_service.get_logs(room_id="room-789")
        
        assert len(logs) == 1
        assert logs[0].room_id == "room-789"
        assert logs[0].changeset_hash == "hash456"
        assert logs[0].mode == ApplyMode.AUTO
    
    def test_get_logs_returns_all_logs(self, audit_service):
        """Test getting all logs without filter."""
        for i in range(3):
            audit_service.log_apply(AuditLogCreate(
                room_id=f"room-{i}",
                changeset_hash=f"hash-{i}",
                applied_by="user@example.com",
                mode=ApplyMode.MANUAL
            ))
        
        logs = audit_service.get_logs()
        assert len(logs) == 3
    
    def test_get_logs_filters_by_room_id(self, audit_service):
        """Test filtering logs by room_id."""
        audit_service.log_apply(AuditLogCreate(
            room_id="room-a",
            changeset_hash="hash-1",
            applied_by="user@example.com",
            mode=ApplyMode.MANUAL
        ))
        audit_service.log_apply(AuditLogCreate(
            room_id="room-b",
            changeset_hash="hash-2",
            applied_by="user@example.com",
            mode=ApplyMode.AUTO
        ))
        
        logs_a = audit_service.get_logs(room_id="room-a")
        logs_b = audit_service.get_logs(room_id="room-b")
        
        assert len(logs_a) == 1
        assert logs_a[0].room_id == "room-a"
        assert len(logs_b) == 1
        assert logs_b[0].room_id == "room-b"


class TestChangesetHash:
    """Tests for changeset hash computation."""
    
    def test_compute_changeset_hash_returns_string(self):
        """Test that hash is returned as string."""
        changeset = {"changes": [{"file": "test.py"}]}
        result = compute_changeset_hash(changeset)
        
        assert isinstance(result, str)
        assert len(result) == 16  # Truncated to 16 chars
    
    def test_compute_changeset_hash_is_deterministic(self):
        """Test that same input produces same hash."""
        changeset = {"changes": [{"file": "test.py", "type": "create_file"}]}
        
        hash1 = compute_changeset_hash(changeset)
        hash2 = compute_changeset_hash(changeset)
        
        assert hash1 == hash2
    
    def test_compute_changeset_hash_different_for_different_input(self):
        """Test that different input produces different hash."""
        changeset1 = {"changes": [{"file": "test1.py"}]}
        changeset2 = {"changes": [{"file": "test2.py"}]}

        hash1 = compute_changeset_hash(changeset1)
        hash2 = compute_changeset_hash(changeset2)

        assert hash1 != hash2


class TestAuditAPI:
    """Tests for audit log API endpoints."""

    def test_log_apply_endpoint(self):
        """Test the log-apply endpoint creates an audit log entry."""
        response = client.post("/audit/log-apply", json={
            "room_id": "test-room-123",
            "summary_id": "summary-456",
            "changeset": {
                "changes": [{"file": "test.py", "type": "create_file"}]
            },
            "applied_by": "test-user@example.com",
            "mode": "manual"
        })

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["entry"]["room_id"] == "test-room-123"
        assert data["entry"]["applied_by"] == "test-user@example.com"
        assert data["entry"]["mode"] == "manual"
        assert "message" in data

    def test_log_apply_endpoint_auto_mode(self):
        """Test log-apply with auto mode."""
        response = client.post("/audit/log-apply", json={
            "room_id": "test-room-auto",
            "changeset": {"changes": []},
            "applied_by": "auto-user@example.com",
            "mode": "auto"
        })

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["entry"]["mode"] == "auto"

    def test_get_logs_endpoint(self):
        """Test getting logs from the API."""
        # First create a log entry
        client.post("/audit/log-apply", json={
            "room_id": "get-logs-test",
            "changeset": {"test": True},
            "applied_by": "user@example.com",
            "mode": "manual"
        })

        # Now get logs
        response = client.get("/audit/logs")

        assert response.status_code == 200
        data = response.json()
        assert "logs" in data
        assert "count" in data
        assert data["count"] >= 1

    def test_get_logs_filter_by_room_id(self):
        """Test filtering logs by room_id."""
        unique_room = "unique-filter-test-room"

        # Create a log entry with unique room_id
        client.post("/audit/log-apply", json={
            "room_id": unique_room,
            "changeset": {"filter": "test"},
            "applied_by": "filter-user@example.com",
            "mode": "manual"
        })

        # Get logs filtered by room_id
        response = client.get(f"/audit/logs?room_id={unique_room}")

        assert response.status_code == 200
        data = response.json()
        assert data["count"] >= 1
        for log in data["logs"]:
            assert log["room_id"] == unique_room
