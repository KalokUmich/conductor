"""Pydantic schemas for audit logging.

This module defines the data structures used for tracking apply operations.
Every time a user applies code changes (manually or automatically), an
audit log entry is created for accountability and debugging.

These schemas are used by:
    - POST /audit/log-apply: Create new audit entries
    - GET /audit/logs: Retrieve audit history
    - AuditLogService: DuckDB storage layer
"""
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class ApplyMode(str, Enum):
    """How the changes were applied.

    Attributes:
        MANUAL: User explicitly clicked "Apply" button.
        AUTO: Changes were auto-applied based on policy approval.
    """
    MANUAL = "manual"
    AUTO = "auto"


class AuditLogEntry(BaseModel):
    """A single audit log entry recording an apply operation.

    This represents a historical record of code changes being applied
    during a collaboration session.

    Attributes:
        room_id: The collaboration session identifier.
        summary_id: Optional reference to the summary (if applicable).
        changeset_hash: SHA-256 hash (truncated) for deduplication.
        applied_by: User ID of who applied the changes.
        mode: Whether it was manual or auto-applied.
        timestamp: When the changes were applied (UTC).
    """
    room_id: str = Field(..., description="Session identifier")
    summary_id: Optional[str] = Field(None, description="Summary reference")
    changeset_hash: str = Field(..., description="Changeset hash")
    applied_by: str = Field(..., description="User who applied")
    mode: ApplyMode = Field(..., description="manual or auto")
    timestamp: datetime = Field(
        default_factory=datetime.utcnow,
        description="When applied (UTC)"
    )


class AuditLogCreate(BaseModel):
    """Input schema for creating a new audit log entry.

    Used internally by the router to create entries. The timestamp
    is automatically set by the service.

    Attributes:
        room_id: The collaboration session identifier.
        summary_id: Optional reference to the summary.
        changeset_hash: Pre-computed hash of the changeset.
        applied_by: User ID of who applied the changes.
        mode: Whether it was manual or auto-applied.
    """
    room_id: str = Field(..., min_length=1, description="Session identifier")
    summary_id: Optional[str] = Field(None, description="Summary reference")
    changeset_hash: str = Field(..., min_length=1, description="Changeset hash")
    applied_by: str = Field(..., min_length=1, description="User who applied")
    mode: ApplyMode = Field(..., description="manual or auto")

