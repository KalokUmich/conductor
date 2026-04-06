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

        source_branch = req.source_branch
        target_branch = req.target_branch

        if not source_branch or not target_branch:
            pr_data = await client.get_pull_request(req.project, req.repo, req.pr_id)
            source_branch = source_branch or pr_data.get("sourceRefName", "").replace("refs/heads/", "")
            target_branch = target_branch or pr_data.get("targetRefName", "").replace("refs/heads/", "")

        # target uses origin/ (remote ref), source is checked out locally
        diff_spec = f"origin/{target_branch}...{source_branch}"
        logger.info("[AzureDevOps] Diff spec: %s", diff_spec)

        # Step 1.5: Fetch + checkout source branch so agents can read files on disk
        workspace_path = getattr(request.app.state, "azure_devops_workspace", None)
        if workspace_path:
            from .workspace import prepare_for_review

            if await prepare_for_review(workspace_path, source_branch, target_branch):
                logger.info("[AzureDevOps] Workspace ready on branch %s", source_branch)
            else:
                logger.warning("[AzureDevOps] Workspace preparation failed — review may be incomplete")

        # Step 2: Run code review via CodeReviewService
        review_service = getattr(request.app.state, "code_review_service", None)
        if not review_service:
            raise HTTPException(
                status_code=503,
                detail="Code review service not initialized.",
            )

        workspace_path = getattr(request.app.state, "azure_devops_workspace", None)
        if not workspace_path:
            raise HTTPException(
                status_code=503,
                detail="No workspace configured for Azure DevOps reviews. "
                "Clone the repo and set azure_devops.workspace_path in settings.",
            )

        result = await review_service.review(
            workspace_path=workspace_path,
            diff_spec=diff_spec,
            max_agents=req.max_agents,
        )

        logger.info(
            "[AzureDevOps] Review complete: %d findings, recommendation=%s",
            len(result.findings),
            result.merge_recommendation,
        )

        # Step 3: Post each finding as inline thread(s)
        # Split findings into per-location comments (Google/CodeRabbit pattern)
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

        # Step 4: Post summary comment (no file context — PR-level)
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

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("[AzureDevOps] Review failed for PR #%d", req.pr_id)
        return AzureDevOpsReviewResponse(
            status="error",
            pr_id=req.pr_id,
            error=str(exc),
        )


@router.get("/status")
async def get_status(request: Request) -> dict:
    """Check if Azure DevOps integration is configured."""
    client = getattr(request.app.state, "azure_devops_client", None)
    return {
        "enabled": client is not None,
        "org_url": client.org_url if client else None,
    }
