"""Code Review API router.

Endpoints:
    POST /api/code-review/review        — run a multi-agent code review
    POST /api/code-review/review/stream  — SSE streaming version
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .service import CodeReviewService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/code-review", tags=["code-review"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CodeReviewRequest(BaseModel):
    room_id: str = Field(..., description="Room / workspace ID.")
    diff_spec: str = Field(
        ...,
        description="Git ref spec, e.g. 'main...feature/branch' or 'HEAD~5'.",
    )
    max_agents: int = Field(
        default=5, ge=1, le=7,
        description="Max specialized agents to run in parallel.",
    )


class FindingResponse(BaseModel):
    title: str
    category: str
    severity: str
    confidence: float
    file: str = ""
    start_line: int = 0
    end_line: int = 0
    evidence: List[str] = []
    risk: str = ""
    suggested_fix: str = ""
    agent: str = ""


class RiskProfileResponse(BaseModel):
    correctness: str = "low"
    concurrency: str = "low"
    security: str = "low"
    reliability: str = "low"
    operational: str = "low"


class AgentResultResponse(BaseModel):
    agent_name: str
    findings_count: int = 0
    tokens_used: int = 0
    iterations: int = 0
    duration_ms: float = 0.0
    error: Optional[str] = None


class CodeReviewResponse(BaseModel):
    diff_spec: str
    pr_summary: str = ""
    risk_profile: Optional[RiskProfileResponse] = None
    findings: List[FindingResponse] = []
    agent_results: List[AgentResultResponse] = []
    files_reviewed: List[str] = []
    total_tokens: int = 0
    total_iterations: int = 0
    total_duration_ms: float = 0.0
    merge_recommendation: str = ""
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def _get_git_workspace_service():
    from app.main import app
    return app.state.git_workspace_service


def _get_agent_provider():
    from app.main import app
    return getattr(app.state, "agent_provider", None)


def _get_classifier_provider():
    from app.main import app
    return getattr(app.state, "classifier_provider", None)


def _get_trace_writer():
    from app.main import app
    return getattr(app.state, "trace_writer", None)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/review", response_model=CodeReviewResponse)
async def code_review(
    req: CodeReviewRequest,
    git_workspace=Depends(_get_git_workspace_service),
    agent_provider=Depends(_get_agent_provider),
    classifier_provider=Depends(_get_classifier_provider),
    trace_writer=Depends(_get_trace_writer),
) -> CodeReviewResponse:
    """Run a multi-agent code review on a PR diff.

    The review uses the same AI provider as summarization.
    Specialized agents (correctness, concurrency, security, reliability,
    test coverage) are dispatched based on the PR's risk profile.
    Budget is dynamically scaled based on PR size.
    """
    if agent_provider is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No AI provider configured.",
        )

    worktree_path = git_workspace.get_worktree_path(req.room_id)
    if worktree_path is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No workspace for room_id={req.room_id!r}.",
        )

    service = CodeReviewService(
        provider=agent_provider,
        classifier_provider=classifier_provider,
        trace_writer=trace_writer,
    )

    result = await service.review(
        workspace_path=str(worktree_path),
        diff_spec=req.diff_spec,
        max_agents=req.max_agents,
    )

    return _to_response(result)


@router.post("/review/stream")
async def code_review_stream(
    req: CodeReviewRequest,
    git_workspace=Depends(_get_git_workspace_service),
    agent_provider=Depends(_get_agent_provider),
    classifier_provider=Depends(_get_classifier_provider),
    trace_writer=Depends(_get_trace_writer),
):
    """SSE streaming version of code review.

    Streams progress events as agents work, then sends the final result.
    Events: agent_start, agent_done, review_complete, error.
    """
    if agent_provider is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No AI provider configured.",
        )

    worktree_path = git_workspace.get_worktree_path(req.room_id)
    if worktree_path is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No workspace for room_id={req.room_id!r}.",
        )

    service = CodeReviewService(
        provider=agent_provider,
        classifier_provider=classifier_provider,
        trace_writer=trace_writer,
    )

    async def event_generator():
        try:
            result = await service.review(
                workspace_path=str(worktree_path),
                diff_spec=req.diff_spec,
                max_agents=req.max_agents,
            )

            # Emit per-agent summaries
            for ar in result.agent_results:
                event_data = {
                    "agent_name": ar.agent_name,
                    "findings_count": len(ar.findings),
                    "tokens_used": ar.tokens_used,
                    "iterations": ar.iterations,
                    "duration_ms": ar.duration_ms,
                    "error": ar.error,
                }
                yield f"event: agent_done\ndata: {json.dumps(event_data)}\n\n"

            # Emit final result
            resp = _to_response(result)
            yield f"event: review_complete\ndata: {resp.model_dump_json()}\n\n"

        except Exception as e:
            logger.error("Code review stream error: %s", e)
            yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _to_response(result) -> CodeReviewResponse:
    """Convert a ReviewResult to the API response model."""
    risk_resp = None
    if result.risk_profile:
        risk_resp = RiskProfileResponse(
            correctness=result.risk_profile.correctness.value,
            concurrency=result.risk_profile.concurrency.value,
            security=result.risk_profile.security.value,
            reliability=result.risk_profile.reliability.value,
            operational=result.risk_profile.operational.value,
        )

    findings_resp = [
        FindingResponse(
            title=f.title,
            category=f.category.value,
            severity=f.severity.value,
            confidence=f.confidence,
            file=f.file,
            start_line=f.start_line,
            end_line=f.end_line,
            evidence=f.evidence,
            risk=f.risk,
            suggested_fix=f.suggested_fix,
            agent=f.agent,
        )
        for f in result.findings
    ]

    agent_resp = [
        AgentResultResponse(
            agent_name=ar.agent_name,
            findings_count=len(ar.findings),
            tokens_used=ar.tokens_used,
            iterations=ar.iterations,
            duration_ms=ar.duration_ms,
            error=ar.error,
        )
        for ar in result.agent_results
    ]

    return CodeReviewResponse(
        diff_spec=result.diff_spec,
        pr_summary=result.pr_summary,
        risk_profile=risk_resp,
        findings=findings_resp,
        agent_results=agent_resp,
        files_reviewed=result.files_reviewed,
        total_tokens=result.total_tokens,
        total_iterations=result.total_iterations,
        total_duration_ms=result.total_duration_ms,
        merge_recommendation=result.merge_recommendation,
        error=result.error,
    )
