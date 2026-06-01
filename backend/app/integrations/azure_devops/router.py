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
from .models import (
    AzureDevOpsRecheckResponse,
    AzureDevOpsReviewRequest,
    AzureDevOpsReviewResponse,
)

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
    2. Run PRBrainOrchestrator on the diff
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

        # PR size band: AI review only makes sense in the middle of the
        # distribution. Below the floor, a careful human reading is faster
        # and cheaper than any LLM review; above the ceiling, a single
        # pass cannot fit the change into model context usefully and the
        # right intervention is to split the PR, not to review it.
        #
        # Out-of-band PRs still get a PR-level comment explaining WHY we
        # skipped — silence would be a bad UX (authors wouldn't know
        # whether the bot is broken or deliberately abstaining).
        _MIN_REVIEW_LINES = 50
        _MAX_REVIEW_LINES = 2200

        # Out-of-band PRs return merge_recommendation="skipped_*" and
        # vote=0 (no vote). An AI "approve" on a PR we deliberately did
        # NOT review would be actively harmful — authors could be
        # misled into thinking the AI had checked the change and signed
        # off. vote=0 leaves the decision to human reviewers.
        if _total_changed < _MIN_REVIEW_LINES:
            logger.info(
                "[AzureDevOps] PR #%d has %d lines — below %d, posting " "human-review nudge and skipping AI review",
                req.pr_id,
                _total_changed,
                _MIN_REVIEW_LINES,
            )
            try:
                await client.create_thread(
                    project=req.project,
                    repo=req.repo,
                    pr_id=req.pr_id,
                    content=_small_pr_skip_message(
                        _total_changed,
                        _MIN_REVIEW_LINES,
                    ),
                )
            except Exception as exc:
                logger.warning(
                    "[AzureDevOps] Failed to post small-PR notice: %s",
                    exc,
                )
            return AzureDevOpsReviewResponse(
                status="ok",
                pr_id=req.pr_id,
                threads_created=0,
                findings_count=0,
                merge_recommendation="skipped_too_small",
                vote=0,
            )

        if _total_changed > _MAX_REVIEW_LINES:
            logger.info(
                "[AzureDevOps] PR #%d has %d lines — above %d, posting "
                "split-recommended notice and skipping AI review",
                req.pr_id,
                _total_changed,
                _MAX_REVIEW_LINES,
            )

            # Phase 7.8.5 — generate an author-friendly split plan using
            # the strong model (same one the coordinator uses). Appends
            # to the generic skip message when available; falls back to
            # the generic message on any failure (no review blocker).
            split_plan: str | None = None
            try:
                from app.code_review.splitter import generate_pr_split_plan

                # Use main clone's diff (no worktree needed for split
                # planning — we only read, never build/test).
                full_diff = _git_diff_text(main_workspace, diff_spec)
                file_count = _count_changed_files(main_workspace, diff_spec)

                strong_provider = getattr(
                    request.app.state,
                    "pr_brain_strong_provider",
                    None,
                )
                if strong_provider and full_diff:
                    split_plan = await generate_pr_split_plan(
                        diff_text=full_diff,
                        pr_title=pr_data.get("title", ""),
                        pr_description=pr_data.get("description", ""),
                        total_lines=_total_changed,
                        file_count=file_count,
                        provider=strong_provider,
                    )
            except Exception as exc:
                logger.warning(
                    "[AzureDevOps] PR #%d split-plan generation failed " "(falling back to generic skip message): %s",
                    req.pr_id,
                    exc,
                )

            skip_content = _large_pr_skip_message(
                _total_changed,
                _MAX_REVIEW_LINES,
            )
            if split_plan:
                skip_content = skip_content + "\n\n" + split_plan

            try:
                await client.create_thread(
                    project=req.project,
                    repo=req.repo,
                    pr_id=req.pr_id,
                    content=skip_content,
                )
            except Exception as exc:
                logger.warning(
                    "[AzureDevOps] Failed to post large-PR notice: %s",
                    exc,
                )
            return AzureDevOpsReviewResponse(
                status="ok",
                pr_id=req.pr_id,
                threads_created=0,
                findings_count=0,
                merge_recommendation="skipped_too_large",
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

            # Fetch Jira tickets + Confluence pages referenced by this PR
            # before invoking the Brain. Readonly clients are optional; if
            # they're unset or no refs are found, ticket_context is "" and
            # the Brain falls back to diff-only reasoning.
            ticket_context = ""
            try:
                from app.integrations.atlassian.enrichment import fetch_pr_atlassian_context

                ticket_context = await fetch_pr_atlassian_context(
                    jira=getattr(request.app.state, "jira_readonly_client", None),
                    confluence=getattr(request.app.state, "confluence_readonly_client", None),
                    source_branch=source_branch,
                    pr_title=pr_data.get("title", "") or "",
                    pr_description=pr_data.get("description", "") or "",
                )
                if ticket_context:
                    logger.info(
                        "[AzureDevOps] Atlassian context fetched: %d chars",
                        len(ticket_context),
                    )
            except Exception as e:
                logger.warning("[AzureDevOps] Atlassian context fetch failed: %s", e)

            # Step 3: Full review via PRBrainOrchestrator. ``task_id`` makes
            # the scratchpad SQLite filename traceable back to this PR when
            # multiple reviews run concurrently.
            task_id = f"ado-{req.project}-pr-{req.pr_id}"
            orchestrator = pr_brain_factory(
                worktree_path,
                diff_spec,
                task_id=task_id,
                pr_title=pr_data.get("title", "") or "",
                pr_description=pr_data.get("description", "") or "",
                ticket_context=ticket_context,
            )

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
            total_cost_usd = 0.0
            total_iterations = 0
            duration_ms = 0.0

            try:
                async for event in orchestrator.run_stream():
                    if event.kind == "done":
                        data = event.data
                        synthesis = data.get("answer", "")
                        merge_rec = data.get("merge_recommendation", "")
                        files_reviewed = data.get("files_reviewed", [])
                        total_iterations = data.get("total_iterations", 0)
                        total_tokens = data.get("total_tokens", 0)
                        total_cost_usd = data.get("total_cost_usd", 0.0)
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
            finally:
                # Phase 9.15 — release the session-scoped Fact Vault.
                orchestrator.cleanup()

            result = ReviewResult(
                diff_spec=diff_spec,
                findings=findings,
                files_reviewed=files_reviewed,
                merge_recommendation=merge_rec,
                synthesis=synthesis,
                total_tokens=total_tokens,
                total_cost_usd=total_cost_usd,
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
            # Translate the Google-style synthesis into Azure-shaped
            # markdown (business-intent first, no code quotes — inline
            # threads already carry code). Strong-model LLM call, ~$0.02.
            # Fail-soft: translator returns the original synthesis on
            # error so the summary still posts.
            try:
                from app.code_review.translate import translate_pr_summary

                agent_provider = getattr(request.app.state, "agent_provider", None)
                translated_summary = result.synthesis
                if agent_provider is not None and result.synthesis:
                    translated_summary = await translate_pr_summary(
                        synthesis=result.synthesis,
                        findings=result.findings,
                        platform="azure",
                        provider=agent_provider,
                        pr_title=pr_data.get("title", "") or "",
                        pr_description=pr_data.get("description", "") or "",
                    )

                summary_md = format_summary_markdown(
                    result,
                    overall_summary_override=translated_summary,
                )
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


@router.post("/recheck", response_model=AzureDevOpsRecheckResponse)
async def recheck_pull_request(
    req: AzureDevOpsReviewRequest,
    request: Request,
) -> AzureDevOpsRecheckResponse:
    """Second-pass re-review: verify the author addressed prior comments, then
    re-review for still-open items + regressions the fixes introduced.

    Flow:
    1. Read all PR comment threads (AI + human; system threads filtered).
    2. Verify each against the CURRENT code — not the thread's status flag.
    3. Re-review the diff with that verified status as context (PR Brain).
    4. Auto-resolve threads confirmed fixed; post new issues inline.
    5. Post a "Second Pass" report + set vote.
    """
    client = _get_client(request)
    start_time = time.time()

    try:
        logger.info(
            "[AzureDevOps] Starting SECOND-PASS recheck for PR #%d in %s/%s",
            req.pr_id,
            req.project,
            req.repo,
        )
        pr_data = await client.get_pull_request(req.project, req.repo, req.pr_id)
        source_branch = req.source_branch or pr_data.get("sourceRefName", "").replace("refs/heads/", "")
        target_branch = req.target_branch or pr_data.get("targetRefName", "").replace("refs/heads/", "")
        diff_spec = f"origin/{target_branch}...origin/{source_branch}"

        main_workspace = getattr(request.app.state, "azure_devops_workspace", None)
        pr_brain_factory = getattr(request.app.state, "pr_brain_factory", None)
        strong_provider = getattr(request.app.state, "pr_brain_strong_provider", None)
        if not main_workspace or not pr_brain_factory or not strong_provider:
            raise HTTPException(status_code=503, detail="Azure DevOps PR Brain not initialized.")

        from .recheck import (
            _RECHECK_FINDING_MARKER,
            build_prior_review_context,
            confirmed_fixed,
            dedupe_findings_against_priors,
            format_recheck_report,
            parse_review_threads,
            still_open,
            verify_prior_comments,
        )

        # Step 1: read prior comments
        raw_threads = await client.list_threads(req.project, req.repo, req.pr_id)
        prior = parse_review_threads(raw_threads)
        logger.info("[AzureDevOps] recheck: %d actionable prior comment(s)", len(prior))
        if not prior:
            try:
                await client.create_thread(
                    project=req.project,
                    repo=req.repo,
                    pr_id=req.pr_id,
                    content=(
                        "## \U0001f916 Conductor AI Code Review — Second Pass\n\n"
                        "No prior review comments were found to re-check. Run the "
                        "first-pass `/review` to generate findings before a recheck."
                    ),
                )
            except Exception as exc:
                logger.warning("[AzureDevOps] recheck: failed to post no-op note: %s", exc)
            return AzureDevOpsRecheckResponse(status="ok", pr_id=req.pr_id, prior_comments=0)

        from .workspace import cleanup_pr_worktree, create_pr_worktree, fetch_latest

        await fetch_latest(main_workspace)
        worktree_path = await create_pr_worktree(main_workspace, source_branch, req.pr_id)
        if not worktree_path:
            raise HTTPException(status_code=500, detail=f"Failed to create worktree for PR #{req.pr_id}")

        try:
            # Step 2: verify prior comments against the CURRENT code
            verdicts = await verify_prior_comments(
                provider=strong_provider,
                comments=prior,
                worktree_path=worktree_path,
                diff_spec=diff_spec,
            )
            logger.info(
                "[AzureDevOps] recheck: verified %d fixed, %d still open (pre-dedupe)",
                len(confirmed_fixed(verdicts)),
                len(still_open(verdicts)),
            )

            # Step 3: re-review with the verified prior status as context
            from app.code_review.models import FindingCategory, ReviewFinding, Severity

            task_id = f"ado-{req.project}-pr-{req.pr_id}-recheck"
            orchestrator = pr_brain_factory(
                worktree_path,
                diff_spec,
                task_id=task_id,
                pr_title=pr_data.get("title", "") or "",
                pr_description=pr_data.get("description", "") or "",
                prior_review_context=build_prior_review_context(verdicts),
            )

            findings: list = []
            merge_rec = ""
            total_cost_usd = 0.0
            try:
                async for event in orchestrator.run_stream():
                    if event.kind == "done":
                        data = event.data
                        merge_rec = data.get("merge_recommendation", "")
                        total_cost_usd = data.get("total_cost_usd", 0.0)
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
            finally:
                orchestrator.cleanup()

            # Step 3.5: fold re-review findings that overlap a prior comment back
            # into that comment. A finding at the same file+nearby line as a prior
            # comment is the SAME issue resurfacing, not new — drop it. If it
            # overlaps one we marked verified-fixed, that's a contradiction: the
            # fix isn't real, so flip the verdict to still-open (won't be resolved).
            findings, verdicts = dedupe_findings_against_priors(findings, verdicts)
            fixed = confirmed_fixed(verdicts)
            open_v = still_open(verdicts)
            logger.info(
                "[AzureDevOps] recheck: after dedupe — %d fixed, %d still open, %d new",
                len(fixed),
                len(open_v),
                len(findings),
            )

            # Step 4: auto-resolve threads we confirmed fixed (reply + mark fixed)
            threads_resolved = 0
            for v in fixed:
                try:
                    await client.reply_to_thread(
                        req.project,
                        req.repo,
                        req.pr_id,
                        v.comment.thread_id,
                        f"✅ Conductor verified this is addressed in the current code "
                        f"— marking resolved.\n\n_{v.reason}_",
                    )
                    await client.update_thread_status(req.project, req.repo, req.pr_id, v.comment.thread_id, status=2)
                    threads_resolved += 1
                except Exception as exc:
                    logger.warning(
                        "[AzureDevOps] recheck: failed to resolve thread %d: %s",
                        v.comment.thread_id,
                        exc,
                    )

            # Step 5: post NEW findings as inline threads
            threads_created = 0
            for finding in findings:
                for comment in split_finding_into_comments(finding):
                    try:
                        await client.create_thread(
                            project=req.project,
                            repo=req.repo,
                            pr_id=req.pr_id,
                            # Tag so a later recheck skips its own output (self-filter).
                            content=comment.content + "\n\n" + _RECHECK_FINDING_MARKER,
                            file_path=comment.file_path,
                            start_line=comment.start_line,
                            end_line=comment.end_line,
                        )
                        threads_created += 1
                    except Exception as exc:
                        logger.warning("[AzureDevOps] recheck: failed to post new finding: %s", exc)

            # Still-open prior items block approval regardless of the re-review's own call.
            if open_v:
                merge_rec = "request_changes"

            # Step 6: post the Second Pass report
            report = format_recheck_report(
                verdicts,
                new_findings_count=len(findings),
                recommendation=merge_rec or ("approve" if not open_v and not findings else "request_changes"),
                total_cost_usd=total_cost_usd,
                duration_ms=(time.time() - start_time) * 1000,
            )
            try:
                await client.create_thread(project=req.project, repo=req.repo, pr_id=req.pr_id, content=report)
                threads_created += 1
            except Exception as exc:
                logger.warning("[AzureDevOps] recheck: failed to post report: %s", exc)

            # Step 7: vote
            vote_value = recommendation_to_vote(merge_rec)
            try:
                await client.vote(req.project, req.repo, req.pr_id, vote_value)
            except Exception as exc:
                logger.warning("[AzureDevOps] recheck: failed to set vote: %s", exc)

            logger.info(
                "[AzureDevOps] PR #%d recheck posted: %d fixed-resolved, %d still-open, "
                "%d new findings, vote=%d, %.1fs",
                req.pr_id,
                threads_resolved,
                len(open_v),
                len(findings),
                vote_value,
                time.time() - start_time,
            )
            return AzureDevOpsRecheckResponse(
                status="ok",
                pr_id=req.pr_id,
                prior_comments=len(prior),
                verified_fixed=len(fixed),
                still_open=len(open_v),
                threads_resolved=threads_resolved,
                new_findings=len(findings),
                threads_created=threads_created,
                merge_recommendation=merge_rec,
                vote=vote_value,
            )
        finally:
            await cleanup_pr_worktree(main_workspace, worktree_path)

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("[AzureDevOps] Recheck failed for PR #%d", req.pr_id)
        return AzureDevOpsRecheckResponse(status="error", pr_id=req.pr_id, error=str(exc))


def _small_pr_skip_message(changed_lines: int, floor: int) -> str:
    """PR-level comment posted when a PR is below the AI-review floor."""
    return (
        "## 🤖 Conductor AI Code Review — skipped\n"
        "\n"
        f"This PR touches **{changed_lines} lines** of change, which is "
        f"below our AI review floor of **{floor}**. For changes this "
        "small, a careful human read is faster and more reliable than "
        "an LLM pass — so we're deferring review to the usual human "
        "reviewer rather than adding machine-generated noise.\n"
        "\n"
        "_No action required from the AI side. Please proceed with "
        "normal peer review._"
    )


def _large_pr_skip_message(changed_lines: int, ceiling: int) -> str:
    """PR-level comment posted when a PR is above the AI-review ceiling."""
    return (
        "## 🤖 Conductor AI Code Review — split recommended\n"
        "\n"
        f"This PR touches **{changed_lines} lines** of change, which is "
        f"above our AI review ceiling of **{ceiling}**. A single review "
        "pass cannot fit a change this large into the model's usable "
        "context — findings would be shallow, and the most valuable "
        "intervention here is to split the PR into reviewable units.\n"
        "\n"
        "**Suggested next step**: break the change into logically "
        "independent commits / PRs (e.g. one per concern: schema "
        "migration, handler logic, tests, docs). Rebase or create "
        "stacked PRs so each piece can be reviewed — by humans and by "
        "AI — with full context.\n"
        "\n"
        "_A dedicated PR-splitting assistant is on the roadmap; until "
        "then this review has been skipped._"
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


def _count_changed_files(worktree_path: str, diff_spec: str) -> int:
    """Return number of changed files for a diff-spec."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "diff", "--name-only"] + diff_spec.split() + ["--"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=15,
        )
        return sum(1 for line in result.stdout.splitlines() if line.strip())
    except Exception:
        return 0


def _git_diff_text(worktree_path: str, diff_spec: str, max_bytes: int = 400_000) -> str:
    """Fetch raw unified diff text; bounded so splitter call stays cheap."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "diff"] + diff_spec.split() + ["--"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        diff = result.stdout or ""
        if len(diff) > max_bytes:
            return diff[:max_bytes] + f"\n\n[...diff truncated at {max_bytes} bytes...]"
        return diff
    except Exception:
        return ""


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
