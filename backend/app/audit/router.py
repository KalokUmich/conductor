"""Audit logging API endpoints.

This module provides HTTP endpoints for logging and retrieving audit records.
Every apply operation (manual or auto) should be logged for accountability
and debugging purposes.

Endpoints:
    POST /audit/log-apply: Record an apply operation
    GET /audit/logs: Retrieve audit log entries

Data Storage:
    Audit logs are stored in a local DuckDB database (audit_logs.duckdb).
    This provides fast queries and easy data export for analysis.

Use Cases:
    - Track who applied what changes and when
    - Debug issues by reviewing change history
    - Compliance and accountability for team leads
    - Distinguish between manual and auto-applied changes
"""
from typing import List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from .schemas import ApplyMode, AuditLogEntry
from .service import AuditLogCreate, AuditLogService, compute_changeset_hash

router = APIRouter(prefix="/audit", tags=["audit"])


# =============================================================================
# Request/Response Models
# =============================================================================


class LogApplyRequest(BaseModel):
    """Request body for logging an apply operation.

    Attributes:
        room_id: Unique identifier for the collaboration session.
        summary_id: Optional reference to the summary that led to this change.
        changeset: The ChangeSet that was applied (as a dict for hashing).
        applied_by: User ID of who applied the changes.
        mode: Whether this was a manual or auto-apply operation.
    """
    room_id: str = Field(..., min_length=1, description="Session identifier")
    summary_id: Optional[str] = Field(None, description="Summary reference")
    changeset: dict = Field(..., description="The applied changeset")
    applied_by: str = Field(..., min_length=1, description="User who applied")
    mode: ApplyMode = Field(..., description="manual or auto")


class LogApplyResponse(BaseModel):
    """Response from the log-apply endpoint.

    Attributes:
        success: True if the log entry was created successfully.
        entry: The created audit log entry (None if failed).
        message: Description of the result or error.
    """
    success: bool = Field(..., description="Whether logging succeeded")
    entry: Optional[AuditLogEntry] = Field(None, description="Created entry")
    message: str = Field(..., description="Result description")


class GetLogsResponse(BaseModel):
    """Response from the get-logs endpoint.

    Attributes:
        logs: List of audit log entries (newest first).
        count: Number of entries returned.
    """
    logs: List[AuditLogEntry] = Field(..., description="Log entries")
    count: int = Field(..., description="Number of entries")


# =============================================================================
# Endpoints
# =============================================================================


@router.post("/log-apply", response_model=LogApplyResponse)
async def log_apply(request: LogApplyRequest) -> LogApplyResponse:
    """Record an apply operation in the audit log.

    This endpoint should be called by the extension after successfully
    applying a changeset. It computes a SHA-256 hash of the changeset
    for deduplication and stores the entry in DuckDB.

    Args:
        request: LogApplyRequest with changeset and metadata.

    Returns:
        LogApplyResponse with created entry or error message.

    Note:
        This endpoint never raises HTTP errors. Failures are returned
        as success=false to avoid disrupting the user workflow.
    """
    try:
        # Compute deterministic hash for deduplication and reference
        changeset_hash = compute_changeset_hash(request.changeset)

        create_entry = AuditLogCreate(
            room_id=request.room_id,
            summary_id=request.summary_id,
            changeset_hash=changeset_hash,
            applied_by=request.applied_by,
            mode=request.mode
        )

        service = AuditLogService.get_instance()
        entry = service.log_apply(create_entry)

        return LogApplyResponse(
            success=True,
            entry=entry,
            message=f"Logged apply operation with hash {changeset_hash}"
        )
    except Exception as e:
        # Log failures gracefully - don't disrupt user workflow
        return LogApplyResponse(
            success=False,
            entry=None,
            message=f"Failed to log apply: {str(e)}"
        )


@router.get("/logs", response_model=GetLogsResponse)
async def get_logs(
    room_id: Optional[str] = None,
    limit: int = 100
) -> GetLogsResponse:
    """Retrieve audit log entries.

    Returns audit logs in reverse chronological order (newest first).
    Can filter by room_id to show only logs from a specific session.

    Args:
        room_id: Optional session ID to filter by.
        limit: Maximum entries to return (default 100, max 1000).

    Returns:
        GetLogsResponse with list of audit entries and count.
    """
    service = AuditLogService.get_instance()
    logs = service.get_logs(room_id=room_id, limit=limit)

    return GetLogsResponse(
        logs=logs,
        count=len(logs)
    )

