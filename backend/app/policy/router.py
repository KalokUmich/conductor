"""Policy evaluation API endpoints.

This module exposes HTTP endpoints for evaluating auto-apply policies.
The extension calls these endpoints to determine whether a ChangeSet
can be automatically applied without user confirmation.

Endpoints:
    POST /policy/evaluate-auto-apply: Check if a ChangeSet passes policy

Usage Flow:
    1. Lead user enables auto-apply toggle in the extension
    2. AI generates a ChangeSet
    3. Extension calls /policy/evaluate-auto-apply
    4. If allowed=true, changes are applied automatically
    5. If allowed=false, user must review and confirm
"""
from typing import List

from fastapi import APIRouter
from pydantic import BaseModel

from app.agent.schemas import ChangeSet
from app.config import get_config
from .auto_apply import AutoApplyPolicy, evaluate_auto_apply

router = APIRouter(prefix="/policy", tags=["policy"])


# =============================================================================
# Request/Response Models
# =============================================================================


class PolicyEvaluationRequest(BaseModel):
    """Request body for policy evaluation.

    Attributes:
        change_set: The ChangeSet to evaluate against policy rules.
    """
    change_set: ChangeSet


class PolicyEvaluationResponse(BaseModel):
    """Response from policy evaluation.

    Attributes:
        allowed: True if auto-apply is permitted for this ChangeSet.
        reasons: List of policy violations (empty if allowed).
        files_count: Number of files affected by the ChangeSet.
        lines_changed: Total lines changed across all files.
    """
    allowed: bool
    reasons: List[str]
    files_count: int
    lines_changed: int


# =============================================================================
# Endpoints
# =============================================================================


@router.post("/evaluate-auto-apply", response_model=PolicyEvaluationResponse)
async def evaluate_auto_apply_endpoint(
    request: PolicyEvaluationRequest
) -> PolicyEvaluationResponse:
    """Evaluate whether a ChangeSet can be auto-applied.

    Checks the ChangeSet against safety policy rules:
        - max_files <= 2
        - max_lines_changed <= 50
        - No changes to forbidden paths (infra/, db/, security/)

    Args:
        request: PolicyEvaluationRequest containing the ChangeSet.

    Returns:
        PolicyEvaluationResponse with evaluation result and statistics.

    Example:
        POST /policy/evaluate-auto-apply
        {
            "change_set": {
                "changes": [...],
                "summary": "Add helper function"
            }
        }

        Response (allowed):
        {"allowed": true, "reasons": [], "files_count": 1, "lines_changed": 10}

        Response (denied):
        {"allowed": false, "reasons": ["Too many files: 5 > 2"], ...}
    """
    config = get_config()
    result = evaluate_auto_apply(request.change_set, config=config)

    # Calculate statistics for the response
    policy = AutoApplyPolicy()
    files_count = len(request.change_set.changes)
    lines_changed = policy._count_lines_changed(request.change_set)

    return PolicyEvaluationResponse(
        allowed=result.allowed,
        reasons=result.reasons,
        files_count=files_count,
        lines_changed=lines_changed
    )

