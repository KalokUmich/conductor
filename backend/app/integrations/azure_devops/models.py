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
    dry_run: bool = Field(
        default=False,
        description=(
            "If true: run the full review but post NOTHING to the PR (no inline "
            "threads, no summary, no vote) and bypass the size gate; return the "
            "findings in the response for local inspection."
        ),
    )


class AzureDevOpsReviewResponse(BaseModel):
    """Response after posting review to Azure DevOps."""

    status: str = "ok"
    pr_id: int = 0
    threads_created: int = 0
    findings_count: int = 0
    merge_recommendation: str = ""
    vote: int = 0
    error: Optional[str] = None
    # Populated only on dry_run=True: the findings that WOULD have been posted.
    findings: list = Field(default_factory=list)


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


class AzureDevOpsAdversarialRecheckRequest(BaseModel):
    """Request to adversarially recheck the vote-driving findings already posted on a PR."""

    project: str = Field(..., description="Project name")
    repo: str = Field(..., description="Repository name or ID")
    pr_id: int = Field(..., description="Pull Request ID")
    source_branch: str = Field("", description="Source branch (optional, read from PR if empty)")
    apply: bool = Field(
        default=False,
        description=(
            "If true: RESOLVE (close + post an evidence note on) threads whose "
            "finding the Opus judge refutes with code-grounded evidence. The vote "
            "is NEVER changed. If false: dry-run — judge and report only."
        ),
    )
    judge_resolved: bool = Field(
        default=False,
        description="If true, also re-judge already-resolved threads (demo/audit). Never re-resolves them.",
    )
    concurrency: int = Field(default=3, ge=1, le=6, description="Max concurrent Opus judges")


class AzureDevOpsAdversarialRecheckResponse(BaseModel):
    """Response after an adversarial finding recheck."""

    status: str = "ok"
    pr_id: int = 0
    findings_judged: int = 0  # vote-driving findings sent to the judge
    refuted: int = 0  # judged false-positive WITH evidence
    held: int = 0  # findings that stand
    threads_resolved: int = 0  # threads actually closed (apply mode)
    applied: bool = False
    report: str = ""  # human-readable before/after
    details: list = Field(default_factory=list)  # per-finding verdict records
    error: Optional[str] = None


class ThreadComment(BaseModel):
    """A single comment in a PR thread (for formatting)."""

    content: str
    file_path: Optional[str] = None
    start_line: Optional[int] = None
    end_line: Optional[int] = None
    status: int = 1  # 1=active
