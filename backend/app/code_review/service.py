"""Code Review orchestration service.

Implements the full review pipeline:
  1. Parse diff into PRContext
  2. Classify risk
  3. Compute dynamic budget based on PR size
  4. Dispatch specialized agents (in parallel) — lightweight model
  5. Merge and dedup findings
  6. Score and rank findings
  7. Synthesis pass — strong model produces the final polished review
  8. Return structured ReviewResult
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import time
from typing import Dict, List, Optional

from app.ai_provider.base import AIProvider

from .agents import AGENT_SPECS, AgentSpec, run_review_agent
from .dedup import dedup_findings
from .diff_parser import parse_diff
from .models import FindingCategory, PRContext, ReviewFinding, ReviewResult, RiskProfile, Severity
from .ranking import score_and_rank
from .risk_classifier import classify_risk

logger = logging.getLogger(__name__)

# Minimum confidence to keep a finding (below this = too speculative)
_MIN_CONFIDENCE = 0.6


# ---------------------------------------------------------------------------
# Post-filter — enforce quality rules before dedup
# ---------------------------------------------------------------------------


def _post_filter(findings: list[ReviewFinding]) -> list[ReviewFinding]:
    """Apply quality rules to raw agent findings.

    Rules:
      1. Drop findings with confidence < _MIN_CONFIDENCE.
      2. Test-coverage findings can never be critical — downgrade to warning.
      3. Findings whose title contains "missing test" are capped at warning.
    """
    result: list[ReviewFinding] = []
    dropped = 0

    for f in findings:
        # Rule 1: confidence floor
        if f.confidence < _MIN_CONFIDENCE:
            dropped += 1
            continue

        # Rule 2: test_coverage agent findings capped at warning
        if f.category == FindingCategory.TEST_COVERAGE and f.severity == Severity.CRITICAL:
            f.severity = Severity.WARNING

        # Rule 3: "missing test" in any agent capped at warning
        if "missing test" in f.title.lower() and f.severity == Severity.CRITICAL:
            f.severity = Severity.WARNING

        result.append(f)

    if dropped:
        logger.info("Post-filter: dropped %d low-confidence findings", dropped)
    return result


# ---------------------------------------------------------------------------
# Dynamic budget calculation
# ---------------------------------------------------------------------------


def _compute_budget_multiplier(pr_context: PRContext) -> float:
    """Compute a budget multiplier based on PR size.

    Small PRs (<500 lines): 0.5x budget (quick review)
    Medium PRs (500-2000 lines): 1.0x budget (standard)
    Large PRs (2000-5000 lines): 1.5x budget
    Very large PRs (5000+ lines): 2.0x budget (if model supports it)
    """
    lines = pr_context.total_changed_lines
    if lines < 500:
        return 0.5
    elif lines < 2000:
        return 1.0
    elif lines < 5000:
        return 1.5
    else:
        return 2.0


def _should_reject_pr(pr_context: PRContext) -> Optional[str]:
    """Check if a PR is too large to review meaningfully.

    Returns a rejection message or None if the PR is reviewable.
    With dynamic budgets, we raise the threshold to 8000 lines.
    """
    if pr_context.total_changed_lines > 8000:
        return (
            f"This PR has {pr_context.total_changed_lines:,} lines of changes "
            f"across {pr_context.file_count} files, which is too large for an "
            f"effective review. Please split it into smaller PRs (ideally < 500 "
            f"lines each).\n\nChanged files:\n"
            + "\n".join(
                f"- `{f.path}` (+{f.additions}/-{f.deletions})"
                for f in pr_context.files[:30]
            )
        )
    return None


# ---------------------------------------------------------------------------
# Pre-fetch diffs (shared across agents)
# ---------------------------------------------------------------------------

# Matches "diff --git a/path b/path" headers in unified diff output
_DIFF_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)$", re.MULTILINE)

# Max chars per individual file diff before truncation
_MAX_FILE_DIFF_CHARS = 8_000
# Max total chars of diff content injected into an agent prompt
_MAX_TOTAL_DIFF_CHARS = 60_000


def _prefetch_diffs(workspace_path: str, diff_spec: str) -> Dict[str, str]:
    """Fetch all file diffs in a single git call and split by file.

    Returns a dict mapping ``file_path → diff_text`` so that each review
    agent can receive only the diffs relevant to its scope, without making
    redundant ``git_diff`` / ``git_diff_files`` tool calls.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--unified=10"] + diff_spec.strip().split(),
            cwd=workspace_path,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            logger.warning("Pre-fetch diff failed: %s", result.stderr[:200])
            return {}
    except Exception as exc:
        logger.warning("Pre-fetch diff error: %s", exc)
        return {}

    full_diff = result.stdout
    if not full_diff:
        return {}

    diffs: Dict[str, str] = {}

    # Split on "diff --git a/X b/Y" headers.  re.split with capture groups
    # returns: [preamble, a1, b1, body1, a2, b2, body2, ...]
    parts = _DIFF_HEADER_RE.split(full_diff)

    for i in range(1, len(parts) - 2, 3):
        a_path = parts[i]
        b_path = parts[i + 1]
        body = parts[i + 2]
        header = f"diff --git a/{a_path} b/{b_path}"
        diffs[b_path] = header + body

    logger.info("Pre-fetched diffs for %d files", len(diffs))
    return diffs


# ---------------------------------------------------------------------------
# Main service
# ---------------------------------------------------------------------------


class CodeReviewService:
    """Orchestrates multi-agent code review.

    Sub-agents use a lightweight model (same as the query classifier) for
    the iterative tool-calling loop.  This keeps TPM low and latency
    fast.  The main provider is available for any future heavy-lifting
    (e.g. a final synthesis pass).

    Args:
        provider: Main AI provider (strong model, e.g. Sonnet 4.6).
            Reserved for orchestration / synthesis.
        classifier_provider: Lightweight model (e.g. Haiku 4.5) used by
            sub-agents for the iterative review loop.  Falls back to
            ``provider`` when not supplied.
        trace_writer: Optional trace persistence for session metrics.
    """

    def __init__(
        self,
        provider: AIProvider,
        classifier_provider: Optional[AIProvider] = None,
        trace_writer=None,
    ) -> None:
        self._provider = provider
        self._classifier_provider = classifier_provider
        # Sub-agents prefer the lightweight model; fall back to the main one
        self._sub_agent_provider = classifier_provider or provider
        self._trace_writer = trace_writer

    async def review(
        self,
        workspace_path: str,
        diff_spec: str,
        max_agents: int = 5,
    ) -> ReviewResult:
        """Run a full multi-agent code review.

        Args:
            workspace_path: Absolute path to the git workspace.
            diff_spec: Git ref spec, e.g. "main...feature/branch".
            max_agents: Maximum number of agents to run in parallel.

        Returns:
            ReviewResult with aggregated findings.
        """
        start_time = time.monotonic()
        logger.info("Starting code review: diff_spec=%s, workspace=%s", diff_spec, workspace_path)

        # Step 1: Parse diff
        pr_context = parse_diff(workspace_path, diff_spec)
        logger.info(
            "PR parsed: %d files, %d lines changed",
            pr_context.file_count, pr_context.total_changed_lines,
        )

        if pr_context.file_count == 0:
            return ReviewResult(
                diff_spec=diff_spec,
                pr_summary="No changes found in the diff.",
                total_duration_ms=(time.monotonic() - start_time) * 1000,
            )

        # Step 1b: Check if PR is too large
        rejection = _should_reject_pr(pr_context)
        if rejection:
            return ReviewResult(
                diff_spec=diff_spec,
                pr_summary=rejection,
                merge_recommendation="request_changes",
                total_duration_ms=(time.monotonic() - start_time) * 1000,
            )

        # Step 2: Classify risk
        risk_profile = classify_risk(pr_context)
        logger.info(
            "Risk profile: correctness=%s, concurrency=%s, security=%s, "
            "reliability=%s, operational=%s",
            risk_profile.correctness.value,
            risk_profile.concurrency.value,
            risk_profile.security.value,
            risk_profile.reliability.value,
            risk_profile.operational.value,
        )

        # Step 2b: Pre-fetch diffs (one git call, shared across all agents)
        file_diffs = _prefetch_diffs(workspace_path, diff_spec)

        # Step 3: Dynamic budget
        budget_multiplier = _compute_budget_multiplier(pr_context)
        logger.info(
            "Budget multiplier: %.1fx (PR has %d lines)",
            budget_multiplier, pr_context.total_changed_lines,
        )

        # Step 4: Select and dispatch agents
        agents_to_run = []
        for spec in AGENT_SPECS:
            # test_coverage always runs
            always = spec.name == "test_coverage"
            if spec.should_run(risk_profile, always_run=always):
                # Apply budget multiplier
                scaled_spec = AgentSpec(
                    name=spec.name,
                    category=spec.category,
                    tools=spec.tools,
                    budget_tokens=int(spec.budget_tokens * budget_multiplier),
                    max_iterations=min(
                        int(spec.max_iterations * budget_multiplier),
                        40,  # hard cap
                    ),
                    risk_dimensions=spec.risk_dimensions,
                )
                agents_to_run.append(scaled_spec)

        # Cap parallel agents
        agents_to_run = agents_to_run[:max_agents]
        logger.info(
            "Dispatching %d agents: %s",
            len(agents_to_run),
            [a.name for a in agents_to_run],
        )

        # Shared semaphore limits concurrent LLM API calls across all
        # parallel agents.  Without this, N agents all hitting Bedrock
        # simultaneously causes throttling.
        #
        # With Haiku 4.5 (~1-2s per call, ~2K input tokens each), two
        # concurrent calls stay well within typical Bedrock TPM limits.
        llm_semaphore = asyncio.Semaphore(2)

        # Run agents in parallel (with pre-fetched diffs).
        # Sub-agents use the lightweight model (classifier_provider) to
        # keep TPM low and latency fast.
        logger.info(
            "Sub-agent model: %s",
            getattr(self._sub_agent_provider, "model_id",
                    getattr(self._sub_agent_provider, "model", "?")),
        )
        agent_tasks = [
            run_review_agent(
                spec=spec,
                pr_context=pr_context,
                risk_profile=risk_profile,
                provider=self._sub_agent_provider,
                workspace_path=workspace_path,
                trace_writer=self._trace_writer,
                file_diffs=file_diffs,
                llm_semaphore=llm_semaphore,
            )
            for spec in agents_to_run
        ]

        agent_results = await asyncio.gather(*agent_tasks)

        # Step 5: Collect all findings
        all_findings = []
        total_tokens = 0
        total_iterations = 0
        for ar in agent_results:
            all_findings.extend(ar.findings)
            total_tokens += ar.tokens_used
            total_iterations += ar.iterations

        # Step 6: Post-processing pipeline
        #   a. Drop low-confidence findings
        #   b. Enforce severity rules (test_coverage ≤ warning)
        #   c. Deduplicate overlapping findings
        #   d. Score, rank, and cap total count
        filtered = _post_filter(all_findings)
        merged = dedup_findings(filtered)
        ranked = score_and_rank(merged, pr_context)

        # Cap total findings to keep the review focused
        _MAX_FINDINGS = 10
        if len(ranked) > _MAX_FINDINGS:
            logger.info(
                "Capping findings from %d to %d", len(ranked), _MAX_FINDINGS,
            )
            ranked = ranked[:_MAX_FINDINGS]

        # Step 7: Determine merge recommendation
        merge_rec = _merge_recommendation(ranked)

        # Build PR summary (structured fallback)
        pr_summary = _build_summary(pr_context, risk_profile, ranked, merge_rec)

        # Step 8: Synthesis pass — strong model produces polished review
        synthesis = await _synthesize_findings(
            provider=self._provider,  # strong model (e.g. Sonnet 4.6)
            pr_context=pr_context,
            risk_profile=risk_profile,
            findings=ranked,
            merge_rec=merge_rec,
            file_diffs=file_diffs,
        )

        duration_ms = (time.monotonic() - start_time) * 1000

        return ReviewResult(
            diff_spec=diff_spec,
            pr_summary=pr_summary,
            risk_profile=risk_profile,
            findings=ranked,
            agent_results=list(agent_results),
            files_reviewed=[f.path for f in pr_context.files],
            total_tokens=total_tokens,
            total_iterations=total_iterations,
            total_duration_ms=duration_ms,
            merge_recommendation=merge_rec,
            synthesis=synthesis,
        )


# ---------------------------------------------------------------------------
# Synthesis prompt — strong model produces polished final review
# ---------------------------------------------------------------------------

_SYNTHESIS_SYSTEM_PROMPT = """\
You are a Google Staff Software Engineer performing the final synthesis of a multi-agent code review. You follow Google's engineering best practices: readability, simplicity, clear naming, small focused changes, thorough testing, and production-hardened code.

You will receive:
1. A list of structured findings from specialized review agents (correctness, security, concurrency, reliability, test coverage).
2. PR metadata (files changed, lines added/deleted, risk profile).
3. Relevant diff snippets.

Your job is to produce a **single, coherent, publication-quality code review** in Markdown, applying the same rigor you would in a Google code review (Critique).

## Rules

1. **Do not invent new issues.** Only discuss findings provided to you. You may re-phrase, re-prioritize, merge, or dismiss findings — but do not add issues the agents did not find.

2. **Severity must be justified.** Critical = provable bug that WILL cause incorrect behavior in production (race condition with data loss, SQL injection, null deref on a guaranteed path). If you cannot prove it with a concrete scenario, downgrade to warning. "Missing tests" is NEVER critical.

3. **Be precise.** Every finding must reference specific file:line locations. Vague claims like "this could be a problem" without pointing to exact code are not acceptable.

4. **Consolidate duplicates.** If multiple agents flagged the same root cause, merge into one finding with the strongest evidence.

5. **Actionable fixes.** Each finding must include a concrete, implementable suggested fix — following Google's standard of "show, don't tell" (not "consider adding error handling" — instead "wrap the `process()` call at line 42 in a try/except that logs the error and returns a 500 response").

6. **Proportional tone.** Small PRs with minor issues should get brief reviews. Don't write 500 words about a nit. Match review depth to actual risk. Follow Google's principle: "a reviewer's first responsibility is to keep the codebase healthy, but be courteous and explain reasoning."

7. **Praise good patterns.** If the code demonstrates good practices (proper error handling, thorough tests, clean abstractions, good naming), briefly acknowledge it — Google culture encourages recognizing good work.

## Output format

```markdown
## Code Review Summary

<1-3 sentence overall assessment>

### Critical Issues
<numbered list, or "None" if no critical issues>

### Warnings
<numbered list, or "None">

### Suggestions & Nits
<numbered list, or "None">

### What's Done Well
<brief positive feedback if applicable>

### Recommendation
<One of: **Approve**, **Approve with follow-ups**, **Request Changes**>
<1 sentence justification>
```
"""


async def _synthesize_findings(
    provider: AIProvider,
    pr_context: PRContext,
    risk_profile: RiskProfile,
    findings: List[ReviewFinding],
    merge_rec: str,
    file_diffs: Dict[str, str],
) -> str:
    """Call the strong model to produce a polished synthesis of agent findings.

    Args:
        provider: Strong model (e.g. Sonnet 4.6) for synthesis.
        pr_context: Parsed PR context.
        risk_profile: Risk assessment.
        findings: Ranked, deduped findings from sub-agents.
        merge_rec: Merge recommendation string.
        file_diffs: Pre-fetched file diffs.

    Returns:
        Polished markdown review string.
    """
    # Build the findings section for the prompt
    findings_text = []
    for i, f in enumerate(findings, 1):
        loc = f.file
        if f.start_line:
            loc += f":{f.start_line}"
            if f.end_line and f.end_line != f.start_line:
                loc += f"-{f.end_line}"
        findings_text.append(
            f"{i}. [{f.severity.value}] {f.title}\n"
            f"   File: {loc}\n"
            f"   Category: {f.category.value}\n"
            f"   Confidence: {f.confidence:.2f}\n"
            f"   Agent: {f.agent}\n"
            f"   Risk: {f.risk}\n"
            f"   Suggested fix: {f.suggested_fix}\n"
            f"   Evidence: {'; '.join(f.evidence[:3]) if f.evidence else 'none'}"
        )

    # Build relevant diff snippets (truncated to fit context)
    diff_snippets = []
    total_diff_chars = 0
    _MAX_SYNTHESIS_DIFF = 30_000
    for f in findings:
        if f.file and f.file in file_diffs and total_diff_chars < _MAX_SYNTHESIS_DIFF:
            snippet = file_diffs[f.file][:4000]
            diff_snippets.append(f"### {f.file}\n```diff\n{snippet}\n```")
            total_diff_chars += len(snippet)

    # Build the prompt
    prompt = f"""\
## PR Metadata
- Diff spec: {pr_context.diff_spec}
- Files changed: {pr_context.file_count}
- Lines: +{pr_context.total_additions}/-{pr_context.total_deletions} ({pr_context.total_changed_lines} total)
- Max risk: {risk_profile.max_risk().value}
- Risk dimensions: correctness={risk_profile.correctness.value}, security={risk_profile.security.value}, concurrency={risk_profile.concurrency.value}, reliability={risk_profile.reliability.value}, operational={risk_profile.operational.value}

## Files Changed
{chr(10).join(f'- {f.path} (+{f.additions}/-{f.deletions}, {f.category.value})' for f in pr_context.files[:30])}

## Agent Findings ({len(findings)} total)
{chr(10).join(findings_text) if findings_text else 'No issues found by any agent.'}

## Preliminary Recommendation: {merge_rec}

## Relevant Diffs
{chr(10).join(diff_snippets) if diff_snippets else 'No diff snippets available.'}

Please produce the final synthesized code review following the output format specified in your instructions.
"""

    logger.info(
        "Synthesis: calling strong model with %d findings, prompt ~%d chars",
        len(findings), len(prompt),
    )

    try:
        # call_model is synchronous — run in executor to avoid blocking
        loop = asyncio.get_event_loop()
        synthesis = await loop.run_in_executor(
            None,
            lambda: provider.call_model(
                prompt=prompt,
                max_tokens=4096,
                system=_SYNTHESIS_SYSTEM_PROMPT,
            ),
        )
        logger.info("Synthesis complete: %d chars", len(synthesis))
        return synthesis
    except Exception as exc:
        logger.warning("Synthesis failed, falling back to structured summary: %s", exc)
        return ""


def _merge_recommendation(findings: list) -> str:
    """Determine merge recommendation based on findings."""
    critical = sum(1 for f in findings if f.severity == Severity.CRITICAL)
    warnings = sum(1 for f in findings if f.severity == Severity.WARNING)

    if critical > 0:
        return "request_changes"
    if warnings >= 3:
        return "request_changes"
    if warnings > 0:
        return "approve_with_followups"
    return "approve"


def _build_summary(
    pr_context: PRContext,
    risk_profile: RiskProfile,
    findings: list,
    merge_rec: str,
) -> str:
    """Build a human-readable review summary."""
    critical = sum(1 for f in findings if f.severity == Severity.CRITICAL)
    warnings = sum(1 for f in findings if f.severity == Severity.WARNING)
    nits = sum(1 for f in findings if f.severity == Severity.NIT)

    rec_emoji = {
        "approve": "Approve",
        "request_changes": "Request Changes",
        "approve_with_followups": "Approve (with follow-ups)",
    }

    lines = [
        f"## Code Review: {pr_context.diff_spec}",
        f"",
        f"**{pr_context.file_count} files** | "
        f"**+{pr_context.total_additions}/-{pr_context.total_deletions} lines** | "
        f"Risk: {risk_profile.max_risk().value}",
        f"",
        f"### Recommendation: {rec_emoji.get(merge_rec, merge_rec)}",
        f"",
    ]

    if critical + warnings + nits == 0:
        lines.append("No issues found. Code looks good!")
    else:
        if critical:
            lines.append(f"- **{critical} critical** issue(s)")
        if warnings:
            lines.append(f"- **{warnings} warning(s)**")
        if nits:
            lines.append(f"- {nits} nit(s)")

    return "\n".join(lines)
