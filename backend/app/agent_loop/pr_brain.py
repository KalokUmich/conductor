"""PR Brain — specialized deterministic pipeline for PR reviews.

Combines the Brain's 4-layer prompt architecture and agent dispatch with
CodeReviewService's proven pre-computation, structured output, and
deterministic post-processing.

Flow:
  Phase 1: Pre-compute (parse diff, classify risk, prefetch diffs, impact graph)
  Phase 2: Dispatch 5 review agents in parallel (via AgentToolExecutor)
  Phase 3: Post-process (evidence gate, post_filter, dedup, score_and_rank)
  Phase 4: Dispatch arbitration agent (pr_arbitrator with tools to verify)
  Phase 5: Merge recommendation (deterministic)
  Phase 6: Synthesis (strong model LLM call)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Dict, List, Optional

from app.ai_provider.base import AIProvider
from app.code_review.dedup import dedup_findings
from app.code_review.diff_parser import parse_diff
from app.code_review.models import (
    FindingCategory,
    PRContext,
    ReviewFinding,
    RiskLevel,
    RiskProfile,
    Severity,
)
from app.code_review.ranking import score_and_rank
from app.code_review.risk_classifier import classify_risk
from app.code_review.shared import (
    AGENT_CATEGORIES,
    FOCUS_DESCRIPTIONS,
    STRATEGY_HINTS,
    build_diffs_section,
    build_impact_context,
    build_summary,
    compute_budget_multiplier,
    evidence_gate,
    extract_relevant_diff,
    merge_recommendation,
    parse_findings,
    post_filter,
    prefetch_diffs,
    repair_output,
    should_reject_pr,
)
from app.code_tools.executor import ToolExecutor
from app.code_tools.schemas import ToolResult
from app.workflow.models import PRBrainConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunable parameters are loaded from config/brains/pr_review.yaml via
# PRBrainConfig.  Only true constants (regex, enum maps) stay here.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------


class WorkflowEvent:
    """Lightweight event container compatible with WorkflowEngine's event queue."""

    def __init__(self, kind: str, data: Dict[str, Any]):
        self.kind = kind
        self.data = data


# Synthesis system prompt — reused from CodeReviewService
_SYNTHESIS_SYSTEM_PROMPT = """\
You are the **final judge** in a multi-agent code review. You receive:
- Findings from specialized review agents (the **prosecution** — evidence FOR each issue)
- Challenge results from an arbitration agent (the **defense** — counter-evidence AGAINST)

Your job is to weigh both sides and produce the definitive review.

## Rules

1. **You decide severity.** The sub-agent's severity is a recommendation. \
The arbitrator's suggested severity is a counter-recommendation. You weigh \
the evidence and counter-evidence to set the final severity.
2. **High rebuttal confidence (>0.7) = likely downgrade or drop.** If the \
arbitrator found concrete counter-evidence, take it seriously.
3. **Low rebuttal confidence (<0.3) = finding is solid.** Keep the sub-agent's severity.
4. **Do not invent new issues.** Only discuss findings provided to you.
5. **Be precise.** Every finding must reference specific file:line locations.
6. **Consolidate duplicates.** Same root cause → one finding.
7. **Actionable fixes.** Concrete implementations, not "consider adding".
8. **Proportional tone.** Match review depth to actual risk.
9. **Praise good patterns** if applicable.

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


@dataclass
class ArbitrationVerdict:
    """Arbitrator's challenge result for one finding.

    Attributes:
        index: Index into the findings list this verdict applies to.
        counter_evidence: Reasons the finding might be wrong or overstated.
        rebuttal_confidence: 0.0 means the finding is solid; 1.0 means it is
            almost certainly wrong.
        suggested_severity: Arbitrator's recommended severity after challenge.
        reason: One-line rationale for the rebuttal assessment.
    """

    index: int
    counter_evidence: List[str] = field(default_factory=list)
    rebuttal_confidence: float = 0.0  # 0.0 = cannot rebut, 1.0 = finding is wrong
    suggested_severity: str = ""  # arbitrator's recommended severity
    reason: str = ""  # one-line rationale


class PRBrainOrchestrator:
    """Deterministic pipeline for PR reviews, dispatching agents via Brain infrastructure.

    This is NOT an LLM loop. The workflow is fixed:
      1. Pre-compute context (deterministic)
      2. Dispatch review agents (LLM, via AgentToolExecutor)
      3. Post-process findings (deterministic)
      4. Dispatch arbitration agent (LLM)
      5. Merge recommendation (deterministic)
      6. Synthesis (LLM)
    """

    def __init__(
        self,
        provider: AIProvider,
        explorer_provider: AIProvider,
        workspace_path: str,
        diff_spec: str,
        pr_brain_config: PRBrainConfig,
        agent_registry: Dict[str, Any],
        tool_executor: ToolExecutor,
        trace_writer=None,
        event_sink: Optional[asyncio.Queue] = None,
    ):
        self._provider = provider
        self._explorer_provider = explorer_provider
        self._workspace_path = workspace_path
        self._diff_spec = diff_spec
        self._config = pr_brain_config
        self._agent_registry = agent_registry
        self._tool_executor = tool_executor
        self._trace_writer = trace_writer
        self._event_sink = event_sink

    async def run_stream(self) -> AsyncGenerator[WorkflowEvent, None]:
        """Execute the full PR review pipeline, yielding progress events.

        Phases:
          1. Parse diff and classify risk (deterministic, no LLM).
          2. Dispatch review agents in parallel.
          3. Post-process findings (filter, dedup, rank).
          4. Arbitration agent challenges each finding.
          5. Merge recommendation (deterministic).
          6. Synthesis via the strong model (final judge).

        Yields:
            WorkflowEvent instances with kinds:
            ``pr_brain_start``, ``pr_context``, ``agents_dispatching``,
            ``agents_complete``, ``post_processing``, ``arbitration_complete``,
            ``done`` (or an early ``done`` on empty diff / oversized PR).
        """
        start_time = time.monotonic()

        logger.info(
            "PR Brain starting: workspace=%s, diff_spec=%s",
            self._workspace_path,
            self._diff_spec,
        )

        yield WorkflowEvent(
            "pr_brain_start",
            {
                "diff_spec": self._diff_spec,
                "workspace_path": self._workspace_path,
            },
        )

        # ------------------------------------------------------------------
        # Phase 1: Pre-compute (deterministic, no LLM calls)
        # ------------------------------------------------------------------

        pr_context = parse_diff(self._workspace_path, self._diff_spec)
        logger.info(
            "PR parsed: %d files, %d lines changed",
            pr_context.file_count,
            pr_context.total_changed_lines,
        )

        if pr_context.file_count == 0:
            yield WorkflowEvent(
                "done",
                {
                    "answer": "No changes found in the diff.",
                    "findings": [],
                    "merge_recommendation": "approve",
                },
            )
            return

        rejection = should_reject_pr(
            pr_context,
            max_lines=self._config.limits.reject_above,
        )
        if rejection:
            yield WorkflowEvent(
                "done",
                {
                    "answer": rejection,
                    "findings": [],
                    "merge_recommendation": "request_changes",
                },
            )
            return

        risk_profile = classify_risk(pr_context)
        file_diffs = prefetch_diffs(self._workspace_path, self._diff_spec)
        impact_context = build_impact_context(self._workspace_path, pr_context)
        budget_multiplier = compute_budget_multiplier(pr_context)

        logger.info(
            "Risk: correctness=%s, concurrency=%s, security=%s, reliability=%s, operational=%s | budget=%.1fx",
            risk_profile.correctness.value,
            risk_profile.concurrency.value,
            risk_profile.security.value,
            risk_profile.reliability.value,
            risk_profile.operational.value,
            budget_multiplier,
        )

        yield WorkflowEvent(
            "pr_context",
            {
                "file_count": pr_context.file_count,
                "total_lines": pr_context.total_changed_lines,
                "budget_multiplier": budget_multiplier,
            },
        )

        # ------------------------------------------------------------------
        # Phase 2: Dispatch review agents
        # ------------------------------------------------------------------

        agents_to_run = self._select_agents(risk_profile, pr_context)
        logger.info("Dispatching %d review agents: %s", len(agents_to_run), agents_to_run)

        yield WorkflowEvent(
            "agents_dispatching",
            {
                "agents": agents_to_run,
                "budget_multiplier": budget_multiplier,
            },
        )

        agent_results = await self._dispatch_agents(
            agents_to_run,
            pr_context,
            risk_profile,
            file_diffs,
            impact_context,
            budget_multiplier,
        )

        yield WorkflowEvent(
            "agents_complete",
            {
                "agent_count": len(agent_results),
            },
        )

        # ------------------------------------------------------------------
        # Phase 3: Post-process (deterministic)
        # ------------------------------------------------------------------

        findings = await self._post_process(agent_results, pr_context)

        yield WorkflowEvent(
            "post_processing",
            {
                "findings_count": len(findings),
            },
        )

        # ------------------------------------------------------------------
        # Phase 3b: Verification — agent with tool access refutes findings
        # Like Claude Code's bughunter "verifying" phase
        # ------------------------------------------------------------------

        if findings:
            findings = await self._verify_findings(findings)
            yield WorkflowEvent(
                "verification_complete",
                {
                    "findings_survived": len(findings),
                },
            )

        # ------------------------------------------------------------------
        # Phase 4: Arbitration agent
        # ------------------------------------------------------------------

        verdicts = []
        if findings:
            verdicts = await self._arbitrate(findings, file_diffs)
            yield WorkflowEvent(
                "arbitration_complete",
                {
                    "findings_count": len(findings),
                    "avg_rebuttal": sum(v.rebuttal_confidence for v in verdicts) / len(verdicts) if verdicts else 0,
                },
            )

        # ------------------------------------------------------------------
        # Phase 5: Merge recommendation (deterministic, based on sub-agent severity)
        # ------------------------------------------------------------------

        merge_rec = merge_recommendation(findings)
        pr_summary = build_summary(pr_context, risk_profile, findings, merge_rec)

        # ------------------------------------------------------------------
        # Phase 6: Synthesis — Brain is the final judge
        # Receives sub-agent findings (pro) + arbitrator verdicts (con)
        # ------------------------------------------------------------------

        synthesis = await self._synthesize(
            pr_context,
            risk_profile,
            findings,
            verdicts,
            merge_rec,
            file_diffs,
        )

        duration_ms = (time.monotonic() - start_time) * 1000  # Convert seconds to milliseconds
        logger.info(
            "PR Brain complete: %d findings, rec=%s, %.0fms",
            len(findings),
            merge_rec,
            duration_ms,
        )

        # Collect token usage from agent results
        total_tokens = 0
        total_iterations = 0
        for result in agent_results:
            if result.success and isinstance(result.data, dict):
                total_iterations += result.data.get("iterations", 0)
                total_tokens += result.data.get("total_input_tokens", 0)
                total_tokens += result.data.get("total_output_tokens", 0)

        yield WorkflowEvent(
            "done",
            {
                "answer": synthesis or pr_summary,
                "findings": [_finding_to_dict(f) for f in findings],
                "files_reviewed": [f.path for f in pr_context.files],
                "merge_recommendation": merge_rec,
                "duration_ms": duration_ms,
                "total_iterations": total_iterations,
                "agents_dispatched": len(agents_to_run),
                "findings_before_arbitration": len(findings)
                + len([f for f in findings if f.severity == Severity.PRAISE]),
            },
        )

    # ------------------------------------------------------------------
    # Phase 2 helpers
    # ------------------------------------------------------------------

    def _select_agents(
        self,
        risk_profile: RiskProfile,
        pr_context: Optional[PRContext] = None,
    ) -> List[str]:
        """Select which review agents to run based on risk profile and PR size.

        Always-run agents (``correctness``, ``test_coverage``) are included
        regardless of risk.  For small PRs (below ``limits.small_pr_threshold``
        lines), ``concurrency`` and ``reliability`` are skipped unless their
        associated risk dimension is HIGH or CRITICAL.

        Args:
            risk_profile: Classified risk levels across five dimensions.
            pr_context: Optional PR metadata used to determine PR size.

        Returns:
            Ordered list of agent names to dispatch.
        """
        agents = []
        risk_triggers = {
            "correctness": [],  # always runs — most important reviewer
            "concurrency": ["concurrency"],
            "security": ["security"],
            "reliability": ["reliability", "operational"],
            "test_coverage": [],  # always runs
        }

        small_pr_threshold = self._config.limits.small_pr_threshold
        is_small_pr = pr_context and pr_context.total_changed_lines < small_pr_threshold

        for name in self._config.review_agents:
            triggers = risk_triggers.get(name, [])
            if not triggers:
                # Always-run agent (test_coverage)
                agents.append(name)
                continue

            # For small PRs, skip concurrency/reliability unless risk is HIGH or CRITICAL
            if is_small_pr and name in ("concurrency", "reliability"):
                high_risk = any(
                    getattr(risk_profile, dim, RiskLevel.LOW) in (RiskLevel.HIGH, RiskLevel.CRITICAL)
                    for dim in triggers
                )
                if not high_risk:
                    logger.info("Small PR: skipping '%s' agent (risk too low)", name)
                    continue

            for dim in triggers:
                level = getattr(risk_profile, dim, RiskLevel.LOW)
                if level in (RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL):
                    agents.append(name)
                    break

        return agents

    async def _dispatch_agents(
        self,
        agents: List[str],
        pr_context: PRContext,
        risk_profile: RiskProfile,
        file_diffs: Dict[str, str],
        impact_context: str,
        budget_multiplier: float,
    ) -> List[ToolResult]:
        """Dispatch review agents in parallel via AgentToolExecutor."""
        from app.workflow.loader import load_brain_config, load_swarm_registry

        from .brain import AgentToolExecutor, BrainBudgetManager
        from .config import BrainExecutorConfig

        brain_config = load_brain_config()
        swarm_registry = load_swarm_registry()

        budget_mgr = BrainBudgetManager(self._config.limits.total_session_tokens)

        # Shared semaphore limits concurrent LLM API calls across all
        # parallel agents — prevents Bedrock throttling.
        llm_semaphore = asyncio.Semaphore(self._config.limits.llm_concurrency_limit)

        # Shared executor config (depth/concurrency/timeout) — provider varies per agent
        _executor_cfg = BrainExecutorConfig(
            workspace_path=self._workspace_path,
            current_depth=0,
            max_depth=self._config.limits.max_depth,
            max_concurrent=self._config.limits.max_concurrent_agents,
            sub_agent_timeout=self._config.limits.sub_agent_timeout,
        )

        # Provider map: agents with model="strong" use the strong provider,
        # others use the explorer (lightweight) provider.
        def _get_provider_for(agent_name: str):
            config = self._agent_registry.get(agent_name)
            if config and getattr(config, "model", "explorer") == "strong":
                return self._provider  # strong model (same as Brain)
            return self._explorer_provider  # lightweight model

        semaphore = asyncio.Semaphore(self._config.limits.max_concurrent_agents)

        async def run_one(agent_name: str) -> ToolResult:
            async with semaphore:
                provider = _get_provider_for(agent_name)
                executor = AgentToolExecutor(
                    inner_executor=self._tool_executor,
                    agent_registry=self._agent_registry,
                    swarm_registry=swarm_registry,
                    agent_provider=provider,
                    config=_executor_cfg,
                    brain_config=brain_config,
                    trace_writer=self._trace_writer,
                    event_sink=self._event_sink,
                    budget_manager=budget_mgr,
                    llm_semaphore=llm_semaphore,
                )
                query = self._build_agent_query(
                    agent_name,
                    pr_context,
                    risk_profile,
                    file_diffs,
                    impact_context,
                )
                weight = self._config.budget_weights.get(agent_name, 1.0)
                return await executor.execute(
                    "dispatch_agent",
                    {
                        "agent_name": agent_name,
                        "query": query,
                        "budget_weight": weight * budget_multiplier,
                    },
                )

        results = await asyncio.gather(
            *[run_one(name) for name in agents],
            return_exceptions=True,
        )

        # Convert exceptions to error ToolResults; tag each with agent_name
        processed = []
        for name, result in zip(agents, results):
            if isinstance(result, Exception):
                logger.error("Review agent '%s' raised: %s", name, result)
                processed.append(
                    ToolResult(
                        tool_name="dispatch_agent",
                        success=False,
                        error=f"Agent '{name}' failed: {result}",
                    )
                )
            else:
                # Tag condensed data with the agent name so _post_process
                # can assign the correct FindingCategory
                if result.success and isinstance(result.data, dict):
                    result.data["agent_name"] = name
                processed.append(result)

        return processed

    def _build_agent_query(
        self,
        agent_name: str,
        pr_context: PRContext,
        risk_profile: RiskProfile,
        file_diffs: Dict[str, str],
        impact_context: str,
    ) -> str:
        """Build the Layer 4 user message for a review sub-agent.

        Contains ONLY task-specific data: PR context, diffs, impact graph,
        per-agent focus directive and strategy hint.  Agent identity (Layer 1)
        and the provability framework (Layer 3) are handled by the agent's
        ``.md`` file and the ``code_review_pr`` skill.

        Args:
            agent_name: Which review agent this message is for (determines
                scoped file list and focus/strategy text).
            pr_context: Parsed PR metadata (files, lines changed, diff spec).
            risk_profile: Classified risk levels used in the prompt summary.
            file_diffs: Pre-fetched per-file diff text (path → diff).
            impact_context: Pre-computed dependency/caller graph for changed files.

        Returns:
            Formatted user-message string ready for injection as Layer 4.
        """
        # Scope files per agent type
        files = pr_context.business_logic_files()
        if agent_name == "test_coverage":
            files = pr_context.files
        elif agent_name == "security":
            files = pr_context.business_logic_files() + pr_context.config_files()

        diffs_section = build_diffs_section(files, file_diffs)

        file_list = "\n".join(f"- `{f.path}` ({f.status}, +{f.additions}/-{f.deletions})" for f in files[:20])

        risk_summary = (
            f"correctness={risk_profile.correctness.value}, "
            f"concurrency={risk_profile.concurrency.value}, "
            f"security={risk_profile.security.value}, "
            f"reliability={risk_profile.reliability.value}, "
            f"operational={risk_profile.operational.value}"
        )

        focus = FOCUS_DESCRIPTIONS.get(agent_name, "General code quality")
        strategy = STRATEGY_HINTS.get(agent_name, "Investigate the highest-impact issues first.")

        impact_section = ""
        if impact_context:
            impact_section = f"\n<impact_context>\n{impact_context}\n</impact_context>\n"

        return f"""\
Review this PR for {agent_name.replace("_", " ")} issues.

## Your Focus
{focus}

## Investigation Strategy
{strategy}

<pr_context>
diff_spec: {pr_context.diff_spec}
files: {pr_context.file_count} ({pr_context.total_changed_lines} lines changed)
risk: {risk_summary}
</pr_context>

<file_list>
{file_list}
</file_list>

<diffs>
{diffs_section}
</diffs>
{impact_section}
## Diff interpretation — understand intent before flagging
When reviewing diffs, distinguish these categories:
- **New code** (entirely new file or new method): Look for bugs in the new logic.
- **Changed code** (old line replaced by new line): Understand WHY it changed.
  Use git_show to see the BEFORE version. If the change is intentional (e.g.,
  POST→GET, gRPC→REST migration, renamed method), do NOT flag the new pattern
  as a defect unless it is provably broken.
- **Moved/refactored code**: If logic is preserved but restructured, do NOT flag
  pre-existing patterns as new issues introduced by this PR.

## Instructions
1. Analyze the diffs above for issues in your focus area.
2. Use **read_file** with line ranges for broader context around changes.
3. Use **git_show** with a commit ref and file path to see the code BEFORE the change.
4. Use additional tools (find_references, get_callers, trace_variable, etc.) to trace impact.
5. The file list and diffs are already provided — skip git_diff_files.
6. When you have enough evidence, stop investigating and produce your findings JSON.
7. **Report at most 5 findings.** Prioritize by real-world impact. One finding per root cause.
"""

    # ------------------------------------------------------------------
    # Phase 3 helpers
    # ------------------------------------------------------------------

    async def _post_process(
        self,
        agent_results: List[ToolResult],
        pr_context: PRContext,
    ) -> List[ReviewFinding]:
        """Run the deterministic post-processing pipeline on raw agent findings.

        Steps (in order):
          1. Parse JSON findings from each agent's answer text.
          2. Apply evidence gate (downgrade under-evidenced criticals).
          3. Cap each agent at its top 3 findings by confidence.
          4. Post-filter (confidence floor + test-coverage severity cap).
          5. Drop test-file-only test_coverage findings when source findings exist.
          6. Dedup (merge overlapping findings from multiple agents).
          7. Score and rank by impact.
          8. Cap at ``max_findings`` from the pipeline config.

        Args:
            agent_results: Raw ToolResult list from dispatched review agents.
            pr_context: PR metadata used during scoring and ranking.

        Returns:
            Ranked list of de-duplicated ReviewFinding objects.
        """
        all_findings: List[ReviewFinding] = []

        for result in agent_results:
            if not result.success or not result.data:
                continue

            data = result.data if isinstance(result.data, dict) else {}
            agent_name = data.get("agent_name", "unknown")
            answer = data.get("answer", "")
            tool_calls_made = data.get("tool_calls_made", 0)
            category = AGENT_CATEGORIES.get(agent_name, FindingCategory.CORRECTNESS)

            # Parse JSON findings from agent answer
            findings = parse_findings(answer, agent_name, category)

            # Repair fallback: if parse failed but answer has substance
            # (>100 chars), make a cheap reformat call to recover findings.
            # This catches truncated outputs from FORCE_CONCLUDE — the agent
            # ran out of budget mid-investigation but still has evidence
            # in its accumulated text.
            if not findings and len(answer) > 100:
                logger.info(
                    "Attempting repair for %s agent (answer=%d chars)",
                    agent_name,
                    len(answer),
                )
                findings = await repair_output(
                    answer, agent_name, category, self._explorer_provider
                )

            # Evidence gate (downgrade under-evidenced criticals)
            if findings:
                findings = evidence_gate(findings, tool_calls_made)

            # Per-agent cap: keep top findings by confidence to reduce false positives
            if len(findings) > self._config.post_processing.max_findings_per_agent:
                findings.sort(key=lambda f: f.confidence, reverse=True)
                logger.info(
                    "Agent '%s' produced %d findings, capping to top %d",
                    agent_name,
                    len(findings),
                    self._config.post_processing.max_findings_per_agent,
                )
                findings = findings[: self._config.post_processing.max_findings_per_agent]

            all_findings.extend(findings)

        logger.info("Raw findings from agents: %d", len(all_findings))

        # Post-filter (confidence floor + severity caps)
        filtered = post_filter(all_findings)

        # Drop test_coverage findings that only point at test files —
        # these are "missing test" observations, not real defects.
        # Keep them only if no source-file finding exists.
        _test_prefixes = ("tests/", "test_", "spec/", "__tests__/")
        source_findings = [
            f for f in filtered if not any(f.file.startswith(p) or f"/{p}" in f.file for p in _test_prefixes)
        ]
        if source_findings:
            before = len(filtered)
            filtered = [
                f
                for f in filtered
                if not (
                    f.category == FindingCategory.TEST_COVERAGE
                    and any(f.file.startswith(p) or f"/{p}" in f.file for p in _test_prefixes)
                )
            ]
            dropped = before - len(filtered)
            if dropped:
                logger.info("Dropped %d test-file-only findings (source findings exist)", dropped)

        # Dedup (merge overlapping findings)
        merged = dedup_findings(filtered)

        # Score and rank
        ranked = score_and_rank(merged, pr_context)

        # Cap at max findings
        max_findings = self._config.post_processing.max_findings
        if len(ranked) > max_findings:
            logger.info("Capping findings from %d to %d", len(ranked), max_findings)
            ranked = ranked[:max_findings]

        return ranked

    # ------------------------------------------------------------------
    # Phase 3b: Verification — tool-enabled agent reads code to confirm/refute
    # ------------------------------------------------------------------

    _VERIFY_PROMPT = """\
You are a **code verification agent**. Your job is to DISPROVE the finding below.
Try your hardest to find counter-evidence that makes this finding invalid.

## The finding to verify
- **Title**: {title}
- **Severity**: {severity}
- **File**: {file}:{start_line}
- **Risk**: {risk}
- **Evidence claimed**:
{evidence}

## Your task
1. Read the actual code at the reported location using read_file.
2. Check for context that would invalidate the finding:
   - Is there a null check, try-catch, or guard clause nearby?
   - Is this an intentional design change visible in the diff?
   - Is the concern already handled by a framework, annotation, or parent caller?
   - Does the caller validate inputs before reaching this code?
3. Render your verdict as JSON:

```json
{{"verdict": "confirmed"|"refuted"|"weakened", "confidence": 0.0-1.0, "reason": "..."}}
```

- **confirmed**: The finding is real and code-provable. State what you verified.
- **refuted**: You found concrete counter-evidence. Cite file:line.
- **weakened**: The finding has some merit but is overstated. Explain why.

Be thorough but fast — you have limited iterations."""

    async def _verify_findings(
        self,
        findings: List[ReviewFinding],
    ) -> List[ReviewFinding]:
        """Run verification agent on each finding in parallel.

        Refuted findings are dropped. Weakened findings get confidence reduced.
        Confirmed findings get a small confidence boost.
        """
        from .budget import BudgetConfig
        from .service import AgentLoopService

        llm_semaphore = asyncio.Semaphore(2)

        async def verify_one(finding: ReviewFinding) -> tuple:
            evidence_lines = "\n".join(f"  - {e}" for e in finding.evidence[:5])
            query = self._VERIFY_PROMPT.format(
                title=finding.title,
                severity=finding.severity.value,
                file=finding.file,
                start_line=finding.start_line,
                risk=finding.risk,
                evidence=evidence_lines,
            )

            budget = BudgetConfig(max_input_tokens=250_000, max_iterations=12)
            agent = AgentLoopService(
                provider=self._explorer_provider,
                max_iterations=12,
                budget_config=budget,
                trace_writer=self._trace_writer,
                _is_sub_agent=True,
                llm_semaphore=llm_semaphore,
            )

            try:
                result = await agent.run(query=query, workspace_path=self._workspace_path)
                answer = result.answer or ""

                verdict = "confirmed"
                confidence = finding.confidence
                reason = ""

                json_match = re.search(r'\{[^}]*"verdict"[^}]*\}', answer, re.DOTALL)
                if json_match:
                    try:
                        vdata = json.loads(json_match.group())
                        verdict = vdata.get("verdict", "confirmed")
                        confidence = float(vdata.get("confidence", finding.confidence))
                        reason = vdata.get("reason", "")
                    except (json.JSONDecodeError, ValueError):
                        pass

                return finding, verdict, confidence, reason
            except Exception as exc:
                logger.warning("Verification failed for '%s': %s", finding.title, exc)
                return finding, "confirmed", finding.confidence, ""

        results = await asyncio.gather(*[verify_one(f) for f in findings])

        verified = []
        for finding, verdict, confidence, reason in results:
            if verdict == "refuted":
                logger.info("Verification: REFUTED '%s' — %s", finding.title, reason)
                continue
            elif verdict == "weakened":
                finding.confidence = min(finding.confidence, confidence)
                finding.reasoning = (finding.reasoning or "") + f"\n[verifier: weakened — {reason}]"
                logger.info(
                    "Verification: WEAKENED '%s' (confidence → %.2f) — %s",
                    finding.title,
                    finding.confidence,
                    reason,
                )
            else:
                finding.confidence = min(finding.confidence + 0.05, 1.0)
                logger.info("Verification: CONFIRMED '%s'", finding.title)
            verified.append(finding)

        logger.info("Verification complete: %d/%d findings survived", len(verified), len(findings))
        return verified

    # ------------------------------------------------------------------
    # Phase 4: Arbitration (adversarial verification)
    #
    # The arbitrator is a defense attorney — it tries to REBUT each
    # finding. It does NOT adjust severity or drop findings. Instead
    # it returns counter-evidence and a suggested severity for each.
    # The synthesis LLM (Brain) sees both sides and makes the final call.
    # ------------------------------------------------------------------

    async def _arbitrate(
        self,
        findings: List[ReviewFinding],
        file_diffs: Dict[str, str],
    ) -> List[ArbitrationVerdict]:
        """Dispatch the arbitration agent to challenge each finding.

        The arbitrator acts as a defense attorney: it tries to REBUT each
        finding and returns counter-evidence plus a suggested severity.  It
        does NOT drop or modify findings — that is the synthesis LLM's job.

        Fast-path: if there are no Critical findings, a single lightweight
        LLM call is used instead of the full tool-enabled arbitrator agent.

        Args:
            findings: Post-processed list of ReviewFinding to challenge.
            file_diffs: Pre-fetched per-file diffs for the arbitrator context.

        Returns:
            One ArbitrationVerdict per finding, in the same order as
            ``findings``.  Missing verdicts are filled with defaults
            (rebuttal_confidence=0.0, reason="not challenged").
        """
        has_critical = any(f.severity == Severity.CRITICAL for f in findings)

        if not has_critical:
            logger.info("No critical findings — using lightweight arbitration")
            return await self._arbitrate_lightweight(findings, file_diffs)

        from app.workflow.loader import load_brain_config, load_swarm_registry

        from .brain import AgentToolExecutor, BrainBudgetManager
        from .config import BrainExecutorConfig

        brain_config = load_brain_config()
        swarm_registry = load_swarm_registry()
        budget_mgr = BrainBudgetManager(self._config.arbitration.budget_tokens)

        executor = AgentToolExecutor(
            inner_executor=self._tool_executor,
            agent_registry=self._agent_registry,
            swarm_registry=swarm_registry,
            agent_provider=self._provider,
            config=BrainExecutorConfig(
                workspace_path=self._workspace_path,
                current_depth=0,
                max_depth=2,
            ),
            brain_config=brain_config,
            trace_writer=self._trace_writer,
            event_sink=self._event_sink,
            budget_manager=budget_mgr,
        )

        findings_data = self._build_findings_for_arbitrator(findings, file_diffs)

        query = (
            f"You are the defense attorney. For each of the {len(findings)} findings below, "
            f"try to REBUT it. Use read_file and grep to verify the cited evidence against "
            f"actual code.\n\n"
            f"For each finding, output:\n"
            f"- counter_evidence: reasons the finding might be wrong or overstated\n"
            f"- rebuttal_confidence: 0.0 (cannot rebut, finding is solid) to 1.0 (finding is wrong)\n"
            f"- suggested_severity: your recommended severity after challenge\n"
            f"- reason: one-line rationale\n\n"
            f"Output a JSON array in <result> tags.\n\n"
            f"{findings_data}"
        )

        logger.info("Dispatching pr_arbitrator with %d findings", len(findings))

        result = await executor.execute(
            "dispatch_agent",
            {
                "agent_name": self._config.arbitrator,
                "query": query,
            },
        )

        if not result.success:
            logger.warning("Arbitrator failed: %s — returning empty verdicts", result.error)
            return self._default_verdicts(findings)

        answer = result.data.get("answer", "") if result.data else ""
        return self._parse_verdicts(findings, answer)

    async def _arbitrate_lightweight(
        self,
        findings: List[ReviewFinding],
        file_diffs: Dict[str, str],
    ) -> List[ArbitrationVerdict]:
        """Lightweight arbitration — single LLM call, no tools."""
        findings_data = self._build_findings_for_arbitrator(findings, file_diffs)

        prompt = (
            f"For each finding below, try to rebut it. Output a JSON array in <result> tags.\n"
            f"Each element: {{index, counter_evidence (array), rebuttal_confidence (0-1), "
            f"suggested_severity, reason}}.\n\n"
            f"{findings_data}"
        )

        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: self._provider.call_model(prompt=prompt, max_tokens=self._config.arbitration.max_tokens),
            )
            return self._parse_verdicts(findings, response)
        except Exception as exc:
            logger.warning("Lightweight arbitration failed: %s", exc)
            return self._default_verdicts(findings)

    def _build_findings_for_arbitrator(
        self,
        findings: List[ReviewFinding],
        file_diffs: Dict[str, str],
    ) -> str:
        """Build the findings + diff context string for arbitration."""
        findings_data = []
        diff_snippets: List[str] = []
        seen_files: set = set()

        for i, f in enumerate(findings):
            loc = f.file
            if f.start_line:
                loc += f":{f.start_line}"
            findings_data.append(
                {
                    "index": i,
                    "title": f.title,
                    "severity": f.severity.value,
                    "confidence": f.confidence,
                    "file": loc,
                    "risk": f.risk,
                    "evidence": f.evidence[:5],
                    "agent": f.agent,
                }
            )

            if f.file and f.file in file_diffs:
                snippet = extract_relevant_diff(file_diffs[f.file], f.start_line, window=80)
                if snippet and f.file not in seen_files:
                    diff_snippets.append(f"### {f.file}\n```diff\n{snippet}\n```")
                    seen_files.add(f.file)

        result = f"<findings>\n{json.dumps(findings_data, indent=2)}\n</findings>\n\n"
        if diff_snippets:
            result += "## Code context\n\n" + "\n\n".join(diff_snippets)
        return result

    def _parse_verdicts(
        self,
        findings: List[ReviewFinding],
        answer: str,
    ) -> List[ArbitrationVerdict]:
        """Parse arbitrator response into ArbitrationVerdict list."""
        result_match = re.search(r"<result>\s*(.*?)\s*</result>", answer, re.DOTALL)
        if not result_match:
            logger.warning("Could not parse arbitrator verdicts")
            return self._default_verdicts(findings)

        try:
            raw = json.loads(result_match.group(1))
        except (json.JSONDecodeError, ValueError):
            logger.warning("Arbitrator verdicts not valid JSON")
            return self._default_verdicts(findings)

        if not isinstance(raw, list):
            return self._default_verdicts(findings)

        verdicts = []
        for item in raw:
            idx = item.get("index", -1)
            if idx < 0 or idx >= len(findings):
                continue
            verdicts.append(
                ArbitrationVerdict(
                    index=idx,
                    counter_evidence=item.get("counter_evidence", []),
                    rebuttal_confidence=float(item.get("rebuttal_confidence", 0.0)),
                    suggested_severity=str(item.get("suggested_severity", findings[idx].severity.value)),
                    reason=item.get("reason", ""),
                )
            )

        # Fill in missing indices with defaults
        covered = {v.index for v in verdicts}
        for i in range(len(findings)):
            if i not in covered:
                verdicts.append(
                    ArbitrationVerdict(
                        index=i,
                        counter_evidence=[],
                        rebuttal_confidence=0.0,
                        suggested_severity=findings[i].severity.value,
                        reason="not challenged",
                    )
                )

        verdicts.sort(key=lambda v: v.index)
        logger.info(
            "Arbitration: %d verdicts, avg rebuttal_confidence=%.2f",
            len(verdicts),
            sum(v.rebuttal_confidence for v in verdicts) / len(verdicts) if verdicts else 0,
        )
        return verdicts

    def _default_verdicts(
        self,
        findings: List[ReviewFinding],
    ) -> List[ArbitrationVerdict]:
        """Return pass-through verdicts when arbitration fails."""
        return [
            ArbitrationVerdict(
                index=i,
                counter_evidence=[],
                rebuttal_confidence=0.0,
                suggested_severity=f.severity.value,
                reason="arbitration unavailable",
            )
            for i, f in enumerate(findings)
        ]

    # ------------------------------------------------------------------
    # Phase 6: Synthesis
    # ------------------------------------------------------------------

    async def _synthesize(
        self,
        pr_context: PRContext,
        risk_profile: RiskProfile,
        findings: List[ReviewFinding],
        verdicts: List[ArbitrationVerdict],
        merge_rec: str,
        file_diffs: Dict[str, str],
    ) -> str:
        """Call the strong model to produce the final polished review.

        Brain acts as the final judge.  It receives sub-agent findings
        (prosecution — evidence FOR each issue) alongside the arbitrator's
        counter-evidence (defense — evidence AGAINST), then decides final
        severity and whether to include each finding.

        Args:
            pr_context: Parsed PR metadata (files, lines, diff spec).
            risk_profile: Risk classification for the five review dimensions.
            findings: Post-processed and ranked findings from review agents.
            verdicts: Arbitration verdicts challenging each finding.
            merge_rec: Deterministic merge recommendation (approve /
                approve_with_followups / request_changes).
            file_diffs: Pre-fetched per-file diffs injected as context.

        Returns:
            Markdown-formatted review string, or an empty string if the LLM
            call fails (caller falls back to ``build_summary``).
        """
        # Build verdict lookup
        verdict_map = {v.index: v for v in verdicts}

        findings_text = []
        for i, f in enumerate(findings):
            loc = f.file
            if f.start_line:
                loc += f":{f.start_line}"
                if f.end_line and f.end_line != f.start_line:
                    loc += f"-{f.end_line}"

            # Sub-agent's case (pro)
            entry = (
                f"{i + 1}. [{f.severity.value}] {f.title}\n"
                f"   File: {loc}\n"
                f"   Category: {f.category.value}\n"
                f"   Agent confidence: {f.confidence:.2f}\n"
                f"   Agent: {f.agent}\n"
                f"   Risk: {f.risk}\n"
                f"   Suggested fix: {f.suggested_fix}\n"
                f"   Evidence FOR: {'; '.join(f.evidence[:3]) if f.evidence else 'none'}"
            )

            # Arbitrator's challenge (con)
            v = verdict_map.get(i)
            if v and (v.counter_evidence or v.rebuttal_confidence > 0.1):
                counter = "; ".join(v.counter_evidence[:3]) if v.counter_evidence else "none"
                entry += (
                    f"\n   --- Arbitrator challenge ---\n"
                    f"   Counter-evidence: {counter}\n"
                    f"   Rebuttal confidence: {v.rebuttal_confidence:.2f}\n"
                    f"   Suggested severity: {v.suggested_severity}\n"
                    f"   Reason: {v.reason}"
                )

            findings_text.append(entry)

        diff_snippets = []
        total_diff_chars = 0
        for f in findings:
            if f.file and f.file in file_diffs and total_diff_chars < self._config.synthesis.max_diff_chars:
                snippet = file_diffs[f.file][: self._config.synthesis.max_diff_snippet_chars]
                diff_snippets.append(f"### {f.file}\n```diff\n{snippet}\n```")
                total_diff_chars += len(snippet)

        prompt = f"""\
<pr_context>
diff_spec: {pr_context.diff_spec}
files_changed: {pr_context.file_count}
lines: +{pr_context.total_additions}/-{pr_context.total_deletions} ({pr_context.total_changed_lines} total)
max_risk: {risk_profile.max_risk().value}
preliminary_recommendation: {merge_rec}
</pr_context>

<file_list>
{chr(10).join(f"- {f.path} (+{f.additions}/-{f.deletions}, {f.category.value})" for f in pr_context.files[:30])}
</file_list>

<findings count="{len(findings)}">
{chr(10).join(findings_text) if findings_text else "No issues found by any agent."}
</findings>

<diffs>
{chr(10).join(diff_snippets) if diff_snippets else "No diff snippets available."}
</diffs>
"""

        logger.info(
            "Synthesis: calling strong model with %d findings, prompt ~%d chars",
            len(findings),
            len(prompt),
        )

        try:
            loop = asyncio.get_event_loop()
            synthesis = await loop.run_in_executor(
                None,
                lambda: self._provider.call_model(
                    prompt=prompt,
                    max_tokens=self._config.synthesis.max_tokens,
                    system=_SYNTHESIS_SYSTEM_PROMPT,
                ),
            )
            logger.info("Synthesis complete: %d chars", len(synthesis))
            return synthesis
        except Exception as exc:
            logger.warning("Synthesis failed: %s", exc)
            return ""


def _finding_to_dict(f: ReviewFinding) -> dict:
    """Convert a ReviewFinding to a serializable dict."""
    return {
        "title": f.title,
        "category": f.category.value,
        "severity": f.severity.value,
        "confidence": f.confidence,
        "file": f.file,
        "start_line": f.start_line,
        "end_line": f.end_line,
        "evidence": f.evidence,
        "risk": f.risk,
        "suggested_fix": f.suggested_fix,
        "agent": f.agent,
    }
