"""Azure DevOps integration router.

POST /api/integrations/azure-devops/review
  → Run PRBrainOrchestrator on the PR diff
  → Post findings as inline PR threads
  → Post summary comment
  → Set vote on PR
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException, Request

from .formatter import format_summary_markdown, recommendation_to_vote, split_finding_into_comments
from .mcp_client import AzureDevOpsClient
from .models import AzureDevOpsReviewRequest, AzureDevOpsReviewResponse

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/integrations/azure-devops",
    tags=["azure-devops"],
)


def _get_client(request: Request) -> AzureDevOpsClient:
    """Get the Azure DevOps client from app state."""
    client = getattr(request.app.state, "azure_devops_client", None)
    if not client:
        raise HTTPException(
            status_code=503,
            detail="Azure DevOps integration is not configured. Set azure_devops.pat in secrets.",
        )
    return client


@router.post("/review", response_model=AzureDevOpsReviewResponse)
async def review_pull_request(
    req: AzureDevOpsReviewRequest,
    request: Request,
) -> AzureDevOpsReviewResponse:
    """Run AI code review on an Azure DevOps PR and post results back.

    Flow:
    1. Read PR metadata (branches)
    2. Run PRBrainOrchestrator (or CodeReviewService) on the diff
    3. Post each finding as an inline PR thread
    4. Post summary comment
    5. Set vote on PR
    """
    client = _get_client(request)
    start_time = time.time()

    try:
        # Step 1: Get PR branches
        logger.info(
            "[AzureDevOps] Starting review for PR #%d in %s/%s",
            req.pr_id,
            req.project,
            req.repo,
        )

        # Always fetch PR metadata (need title + existing description for summary)
        pr_data = await client.get_pull_request(req.project, req.repo, req.pr_id)
        source_branch = req.source_branch or pr_data.get("sourceRefName", "").replace("refs/heads/", "")
        target_branch = req.target_branch or pr_data.get("targetRefName", "").replace("refs/heads/", "")

        # Worktree uses detached HEAD at origin/source — both refs use origin/
        diff_spec = f"origin/{target_branch}...origin/{source_branch}"
        logger.info("[AzureDevOps] Diff spec: %s", diff_spec)

        main_workspace = getattr(request.app.state, "azure_devops_workspace", None)
        if not main_workspace:
            raise HTTPException(
                status_code=503,
                detail="No workspace configured for Azure DevOps reviews.",
            )

        pr_brain_factory = getattr(request.app.state, "pr_brain_factory", None)
        if not pr_brain_factory:
            raise HTTPException(
                status_code=503,
                detail="PR Brain not initialized.",
            )

        # Step 1.5: Fetch latest refs + check diff size on main clone (no worktree needed)
        from .workspace import cleanup_pr_worktree, create_pr_worktree, fetch_latest

        await fetch_latest(main_workspace)
        _total_changed = _count_changed_lines(main_workspace, diff_spec)
        _MIN_REVIEW_LINES = 30

        if _total_changed < _MIN_REVIEW_LINES:
            logger.info(
                "[AzureDevOps] PR #%d has %d lines — below %d, skipping",
                req.pr_id,
                _total_changed,
                _MIN_REVIEW_LINES,
            )
            return AzureDevOpsReviewResponse(
                status="ok",
                pr_id=req.pr_id,
                threads_created=0,
                findings_count=0,
                merge_recommendation="approve",
                vote=0,
            )

        # Step 2: Create worktree (only for PRs worth reviewing)
        worktree_path = await create_pr_worktree(main_workspace, source_branch, req.pr_id)
        if not worktree_path:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to create worktree for PR #{req.pr_id}",
            )

        try:
            # Step 3: Generate PR summary (Haiku, ~5s — failure won't block review)
            await _generate_and_post_summary(
                client=client,
                request=request,
                project=req.project,
                repo=req.repo,
                pr_id=req.pr_id,
                pr_title=pr_data.get("title", ""),
                source_branch=source_branch,
                worktree_path=worktree_path,
                diff_spec=diff_spec,
            )

            # Step 3: Full review via PRBrainOrchestrator
            orchestrator = pr_brain_factory(worktree_path, diff_spec)

            # Collect results from the streaming pipeline
            from app.code_review.models import (
                FindingCategory,
                ReviewFinding,
                ReviewResult,
                Severity,
            )

            findings = []
            synthesis = ""
            merge_rec = ""
            files_reviewed = []
            total_tokens = 0
            total_iterations = 0
            duration_ms = 0.0

            async for event in orchestrator.run_stream():
                if event.kind == "done":
                    data = event.data
                    synthesis = data.get("answer", "")
                    merge_rec = data.get("merge_recommendation", "")
                    files_reviewed = data.get("files_reviewed", [])
                    total_iterations = data.get("total_iterations", 0)
                    duration_ms = data.get("duration_ms", 0.0)
                    for fd in data.get("findings", []):
                        try:
                            findings.append(
                                ReviewFinding(
                                    title=fd.get("title", ""),
                                    category=FindingCategory(fd.get("category", "correctness")),
                                    severity=Severity(fd.get("severity", "warning")),
                                    confidence=fd.get("confidence", 0.7),
                                    file=fd.get("file", ""),
                                    start_line=fd.get("start_line", 0),
                                    end_line=fd.get("end_line", 0),
                                    evidence=fd.get("evidence", []),
                                    risk=fd.get("risk", ""),
                                    suggested_fix=fd.get("suggested_fix", ""),
                                    agent=fd.get("agent", ""),
                                )
                            )
                        except Exception:
                            continue

            result = ReviewResult(
                diff_spec=diff_spec,
                findings=findings,
                files_reviewed=files_reviewed,
                merge_recommendation=merge_rec,
                synthesis=synthesis,
                total_tokens=total_tokens,
                total_iterations=total_iterations,
                total_duration_ms=duration_ms,
            )

            logger.info(
                "[AzureDevOps] Review complete: %d findings, recommendation=%s",
                len(result.findings),
                result.merge_recommendation,
            )

            # Step 3: Post each finding as inline thread(s)
            threads_created = 0
            for finding in result.findings:
                inline_comments = split_finding_into_comments(finding)
                for comment in inline_comments:
                    try:
                        await client.create_thread(
                            project=req.project,
                            repo=req.repo,
                            pr_id=req.pr_id,
                            content=comment.content,
                            file_path=comment.file_path,
                            start_line=comment.start_line,
                            end_line=comment.end_line,
                        )
                        threads_created += 1
                    except Exception as exc:
                        logger.warning(
                            "[AzureDevOps] Failed to create thread for finding '%s' at line %s: %s",
                            finding.title,
                            comment.start_line,
                            exc,
                        )

            # Step 4: Post summary comment
            try:
                summary_md = format_summary_markdown(result)
                await client.create_thread(
                    project=req.project,
                    repo=req.repo,
                    pr_id=req.pr_id,
                    content=summary_md,
                )
                threads_created += 1
            except Exception as exc:
                logger.warning("[AzureDevOps] Failed to post summary: %s", exc)

            # Step 5: Set vote
            vote_value = recommendation_to_vote(result.merge_recommendation)
            try:
                await client.vote(req.project, req.repo, req.pr_id, vote_value)
            except Exception as exc:
                logger.warning("[AzureDevOps] Failed to set vote: %s", exc)

            duration = time.time() - start_time
            logger.info(
                "[AzureDevOps] PR #%d review posted: %d threads, vote=%d, %.1fs",
                req.pr_id,
                threads_created,
                vote_value,
                duration,
            )

            return AzureDevOpsReviewResponse(
                status="ok",
                pr_id=req.pr_id,
                threads_created=threads_created,
                findings_count=len(result.findings),
                merge_recommendation=result.merge_recommendation,
                vote=vote_value,
            )
        finally:
            # Always clean up the worktree, even if review fails
            await cleanup_pr_worktree(main_workspace, worktree_path)

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("[AzureDevOps] Review failed for PR #%d", req.pr_id)
        return AzureDevOpsReviewResponse(
            status="error",
            pr_id=req.pr_id,
            error=str(exc),
        )


def _count_changed_lines(worktree_path: str, diff_spec: str) -> int:
    """Count total insertions + deletions from git diff --shortstat."""
    import re
    import subprocess

    try:
        result = subprocess.run(
            ["git", "diff", "--shortstat"] + diff_spec.split() + ["--"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=15,
        )
        # " 3 files changed, 12 insertions(+), 5 deletions(-)"
        nums = re.findall(r"(\d+) insertion|(\d+) deletion", result.stdout)
        return sum(int(n) for pair in nums for n in pair if n)
    except Exception:
        return 999  # fail open — run review if we can't count


async def _generate_and_post_summary(
    client: AzureDevOpsClient,
    request: Request,
    project: str,
    repo: str,
    pr_id: int,
    pr_title: str,
    source_branch: str,
    worktree_path: str,
    diff_spec: str,
) -> None:
    """Generate AI summary and append to PR description.

    Uses the explorer provider (Haiku) for speed — summary is ready in ~5s,
    well before the full review finishes.
    """
    import subprocess

    from .summarizer import (
        AI_SUMMARY_MARKER,
        build_description_with_summary,
        generate_pr_summary,
    )

    # Skip if this PR already has an AI summary (idempotent)
    try:
        pr_data = await client.get_pull_request(project, repo, pr_id)
        existing_desc = pr_data.get("description", "") or ""
        if AI_SUMMARY_MARKER in existing_desc:
            logger.info("[AzureDevOps] PR #%d already has AI summary — skipping", pr_id)
            return
    except Exception as exc:
        logger.warning("[AzureDevOps] Failed to check PR description: %s", exc)
        return

    explorer = getattr(request.app.state, "explorer_provider", None)
    if not explorer:
        logger.info("[AzureDevOps] No explorer provider — skipping PR summary")
        return

    # Get diff text from the worktree
    try:
        result = subprocess.run(
            ["git", "diff", "--stat"] + diff_spec.split() + ["--"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        diff_stat = result.stdout

        result = subprocess.run(
            ["git", "diff", "--unified=3"] + diff_spec.split() + ["--"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        diff_text = result.stdout
    except Exception as exc:
        logger.warning("[AzureDevOps] Failed to get diff for summary: %s", exc)
        return

    summary = await generate_pr_summary(
        provider=explorer,
        diff_text=f"{diff_stat}\n\n{diff_text}",
        pr_title=pr_title,
        source_branch=source_branch,
    )

    if not summary:
        return

    # Append summary to existing description (existing_desc already fetched above)
    try:
        new_desc = build_description_with_summary(existing_desc, summary)
        await client.update_pr_description(project, repo, pr_id, new_desc)
        logger.info("[AzureDevOps] PR #%d description updated with AI summary", pr_id)
    except Exception as exc:
        logger.warning("[AzureDevOps] Failed to update PR description: %s", exc)


@router.get("/status")
async def get_status(request: Request) -> dict:
    """Check if Azure DevOps integration is configured."""
    client = getattr(request.app.state, "azure_devops_client", None)
    return {
        "enabled": client is not None,
        "org_url": client.org_url if client else None,
    }
