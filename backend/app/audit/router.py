"""Audit logging API endpoints."""

from typing import List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from .schemas import ApplyMode, AuditLogEntry
from .service import AuditLogCreate, AuditLogService, compute_changeset_hash

router = APIRouter(prefix="/audit", tags=["audit"])


class LogApplyRequest(BaseModel):
    room_id: str = Field(..., min_length=1, description="Session identifier")
    summary_id: Optional[str] = Field(None, description="Summary reference")
    changeset: dict = Field(..., description="The applied changeset")
    applied_by: str = Field(..., min_length=1, description="User who applied")
    mode: ApplyMode = Field(..., description="manual or auto")


class LogApplyResponse(BaseModel):
    success: bool = Field(..., description="Whether logging succeeded")
    entry: Optional[AuditLogEntry] = Field(None, description="Created entry")
    message: str = Field(..., description="Result description")


class GetLogsResponse(BaseModel):
    logs: List[AuditLogEntry] = Field(..., description="Log entries")
    count: int = Field(..., description="Number of entries")


@router.post("/log-apply", response_model=LogApplyResponse)
async def log_apply(request: LogApplyRequest) -> LogApplyResponse:
    """Record an apply operation in the audit log."""
    try:
        changeset_hash = compute_changeset_hash(request.changeset)
        create_entry = AuditLogCreate(
            room_id=request.room_id,
            summary_id=request.summary_id,
            changeset_hash=changeset_hash,
            applied_by=request.applied_by,
            mode=request.mode,
        )
        service = AuditLogService.get_instance()
        entry = await service.log_apply(create_entry)
        return LogApplyResponse(
            success=True,
            entry=entry,
            message=f"Logged apply operation with hash {changeset_hash}",
        )
    except Exception as e:
        return LogApplyResponse(
            success=False,
            entry=None,
            message=f"Failed to log apply: {e!s}",
        )


@router.get("/logs", response_model=GetLogsResponse)
async def get_logs(
    room_id: Optional[str] = None,
    limit: int = 100,
) -> GetLogsResponse:
    """Retrieve audit log entries."""
    service = AuditLogService.get_instance()
    logs = await service.get_logs(room_id=room_id, limit=limit)
    return GetLogsResponse(logs=logs, count=len(logs))
