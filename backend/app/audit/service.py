"""DuckDB-based audit log storage service.

This module provides persistent storage for audit log entries using DuckDB,
a fast embedded analytical database. The service implements the singleton
pattern to ensure only one database connection exists at a time.

Database Schema:
    audit_logs table:
        - id: Auto-incrementing primary key
        - room_id: Session/room identifier
        - summary_id: Optional reference to the summary
        - changeset_hash: SHA-256 hash of the applied changeset
        - applied_by: User ID who applied the change
        - mode: 'manual' or 'auto'
        - timestamp: When the change was applied (UTC)

Thread Safety:
    The DuckDB connection is NOT thread-safe. In production with multiple
    workers, each process will have its own connection to the same file.
    DuckDB handles concurrent file access internally.

Usage:
    service = AuditLogService.get_instance()
    entry = service.log_apply(create_entry)
    logs = service.get_logs(room_id="abc-123")
"""
import hashlib
import json
from datetime import datetime
from typing import List, Optional

import duckdb

from .schemas import ApplyMode, AuditLogCreate, AuditLogEntry


class AuditLogService:
    """Singleton service for managing audit logs in DuckDB.

    This service provides methods to log apply operations and retrieve
    audit history. It maintains a single database connection for the
    lifetime of the process.

    Attributes:
        _instance: Singleton instance of the service.
        _db_path: Path to the DuckDB database file.
    """

    _instance: Optional["AuditLogService"] = None
    _db_path: str = "audit_logs.duckdb"

    def __init__(self, db_path: Optional[str] = None) -> None:
        """Initialize the audit log service.

        Creates the database file and schema if they don't exist.

        Args:
            db_path: Path to DuckDB file. Defaults to "audit_logs.duckdb".
        """
        if db_path:
            self._db_path = db_path
        self._connection: Optional[duckdb.DuckDBPyConnection] = None
        self._initialize_db()

    @classmethod
    def get_instance(cls, db_path: Optional[str] = None) -> "AuditLogService":
        """Get or create the singleton instance.

        Args:
            db_path: Optional database path (only used on first call).

        Returns:
            The singleton AuditLogService instance.
        """
        if cls._instance is None:
            cls._instance = cls(db_path)
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset the singleton instance.

        Closes the database connection and clears the instance.
        Primarily used for testing to ensure a clean state.
        """
        if cls._instance is not None:
            cls._instance.close()
            cls._instance = None

    def _get_connection(self) -> duckdb.DuckDBPyConnection:
        """Get the DuckDB connection, creating if needed.

        Returns:
            Active DuckDB connection.
        """
        if self._connection is None:
            self._connection = duckdb.connect(self._db_path)
        return self._connection

    def _initialize_db(self) -> None:
        """Initialize the database schema.

        Creates the audit_logs table and sequence if they don't exist.
        Safe to call multiple times (idempotent).
        """
        conn = self._get_connection()
        conn.execute("""
            CREATE SEQUENCE IF NOT EXISTS audit_logs_seq START 1;
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER DEFAULT nextval('audit_logs_seq') PRIMARY KEY,
                room_id VARCHAR NOT NULL,
                summary_id VARCHAR,
                changeset_hash VARCHAR NOT NULL,
                applied_by VARCHAR NOT NULL,
                mode VARCHAR NOT NULL,
                timestamp TIMESTAMP NOT NULL
            )
        """)
    
    def log_apply(self, entry: AuditLogCreate) -> AuditLogEntry:
        """Log an apply operation.
        
        Args:
            entry: The audit log entry to create.
            
        Returns:
            The created audit log entry with timestamp.
        """
        timestamp = datetime.utcnow()
        conn = self._get_connection()
        conn.execute(
            """
            INSERT INTO audit_logs (room_id, summary_id, changeset_hash, applied_by, mode, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                entry.room_id,
                entry.summary_id,
                entry.changeset_hash,
                entry.applied_by,
                entry.mode.value,
                timestamp
            ]
        )
        
        return AuditLogEntry(
            room_id=entry.room_id,
            summary_id=entry.summary_id,
            changeset_hash=entry.changeset_hash,
            applied_by=entry.applied_by,
            mode=entry.mode,
            timestamp=timestamp
        )
    
    def get_logs(
        self,
        room_id: Optional[str] = None,
        limit: int = 100
    ) -> List[AuditLogEntry]:
        """Get audit logs, optionally filtered by room_id.
        
        Args:
            room_id: Optional room ID to filter by.
            limit: Maximum number of logs to return.
            
        Returns:
            List of audit log entries.
        """
        conn = self._get_connection()
        
        if room_id:
            result = conn.execute(
                """
                SELECT room_id, summary_id, changeset_hash, applied_by, mode, timestamp
                FROM audit_logs
                WHERE room_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                [room_id, limit]
            ).fetchall()
        else:
            result = conn.execute(
                """
                SELECT room_id, summary_id, changeset_hash, applied_by, mode, timestamp
                FROM audit_logs
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                [limit]
            ).fetchall()
        
        return [
            AuditLogEntry(
                room_id=row[0],
                summary_id=row[1],
                changeset_hash=row[2],
                applied_by=row[3],
                mode=ApplyMode(row[4]),
                timestamp=row[5]
            )
            for row in result
        ]
    
    def close(self) -> None:
        """Close the database connection."""
        if self._connection is not None:
            self._connection.close()
            self._connection = None


def compute_changeset_hash(changeset: dict) -> str:
    """Compute a hash for a changeset.
    
    Args:
        changeset: The changeset dictionary to hash.
        
    Returns:
        SHA-256 hash of the changeset.
    """
    # Sort keys for deterministic hashing
    changeset_str = json.dumps(changeset, sort_keys=True)
    return hashlib.sha256(changeset_str.encode()).hexdigest()[:16]

