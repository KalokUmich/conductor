"""Pydantic models for Azure DevOps integration."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class AzureDevOpsReviewRequest(BaseModel):
    """Request to run a PR review and post results to Azure DevOps."""

    org: str = Field(..., description="Azure DevOps organization URL (e.g. https://dev.azure.com/myorg)")
    project: str = Field(..., description="Project name")
    repo: str = Field(..., description="Repository name or ID")
    pr_id: int = Field(..., description="Pull Request ID")
    source_branch: str = Field("", description="Source branch (optional, read from PR if empty)")
    target_branch: str = Field("", description="Target branch (optional, read from PR if empty)")
    max_agents: int = Field(default=6, ge=1, le=8, description="Max review agents (incl. correctness_b)")


class AzureDevOpsReviewResponse(BaseModel):
    """Response after posting review to Azure DevOps."""

    status: str = "ok"
    pr_id: int = 0
    threads_created: int = 0
    findings_count: int = 0
    merge_recommendation: str = ""
    vote: int = 0
    error: Optional[str] = None


class AzureDevOpsRecheckResponse(BaseModel):
    """Response after a second-pass re-check of a PR."""

    status: str = "ok"
    pr_id: int = 0
    prior_comments: int = 0  # actionable prior comments considered
    verified_fixed: int = 0  # confirmed addressed in current code
    still_open: int = 0  # prior comments not (yet) addressed
    threads_resolved: int = 0  # threads auto-marked resolved
    new_findings: int = 0  # brand-new issues found this pass
    threads_created: int = 0  # inline threads posted for new issues
    merge_recommendation: str = ""
    vote: int = 0
    error: Optional[str] = None


class ThreadComment(BaseModel):
    """A single comment in a PR thread (for formatting)."""

    content: str
    file_path: Optional[str] = None
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    status: int = 1  # 1=active
