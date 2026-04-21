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
from pathlib import Path
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
    parse_findings_with_status,
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

# Wall-clock cap (seconds) for the Phase 2 existence-check worker. The
# worker does a handful of greps to verify newly-referenced symbols;
# prompt-level budgets are hints that LLM workers often ignore on large
# codebases (~10 min hangs observed on 17K-file repos). This is the
# hard orchestrator guard — after this deadline, the task is cancelled
# and the coordinator proceeds without Phase 2 facts.
_PHASE2_TIMEOUT_SECONDS = 120


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
        scratchpad=None,
        task_id: Optional[str] = None,
        pr_title: str = "",
        pr_description: str = "",
    ):
        self._provider = provider
        self._explorer_provider = explorer_provider
        self._workspace_path = workspace_path
        self._diff_spec = diff_spec
        self._config = pr_brain_config
        self._agent_registry = agent_registry
        self._trace_writer = trace_writer
        self._event_sink = event_sink
        self._task_id = task_id
        # PR intent — plumbed from caller; coordinator surfaces in user
        # message so agents can check "does this PR actually do what it
        # claims?" not just "is this diff pattern-wise suspicious?".
        self._pr_title = pr_title or ""
        self._pr_description = pr_description or ""

        # Phase 9.15 — task-scoped Fact Vault. Sub-agent tool calls are
        # routed through a CachedToolExecutor so identical grep / read_file /
        # find_symbol queries across 7 parallel review agents hit the vault
        # instead of re-running. Opt out via CONDUCTOR_SCRATCHPAD_ENABLED=0.
        #
        # ``task_id`` (e.g. "ado-pr-12345", "greptile-sentry-006") is folded
        # into the session_id so concurrent PR reviews produce readable
        # scratchpad filenames — isolation was already guaranteed by
        # per-session files, this just makes them traceable.
        import os as _os
        import re as _re
        import uuid as _uuid

        from app.scratchpad import CachedToolExecutor, FactStore

        self._owns_scratchpad = False
        if _os.environ.get("CONDUCTOR_SCRATCHPAD_ENABLED", "1") != "0" and scratchpad is None:
            if task_id:
                slug = _re.sub(r"[^A-Za-z0-9._-]+", "-", task_id).strip("-")[:48] or "pr"
                session_id = f"{slug}-{_uuid.uuid4().hex[:8]}"
            else:
                session_id = f"pr-{_uuid.uuid4().hex[:12]}"
            scratchpad = FactStore.open(
                session_id, workspace=workspace_path, task_id=task_id
            )
            self._owns_scratchpad = True
        self._scratchpad = scratchpad

        # Token returned by contextvars.ContextVar.set so cleanup() can
        # reset binding exactly once, even if cleanup is called twice.
        self._scratchpad_ctx_token = None
        if scratchpad is not None:
            from app.scratchpad.context import _current_store

            self._scratchpad_ctx_token = _current_store.set(scratchpad)
            self._tool_executor = CachedToolExecutor(tool_executor, scratchpad)
        else:
            self._tool_executor = tool_executor

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
        # Attach PR intent so downstream agents see "what this PR is
        # supposed to do" not just raw diff bytes. See __init__.
        pr_context.title = self._pr_title
        pr_context.description = self._pr_description
        logger.info(
            "PR parsed: %d files, %d lines changed, title=%r",
            pr_context.file_count,
            pr_context.total_changed_lines,
            (pr_context.title[:80] if pr_context.title else "(none)"),
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
        # Phase 2 (v2 branch): Brain-as-coordinator dispatch loop
        # ------------------------------------------------------------------
        # When CONDUCTOR_PR_BRAIN_V2=1, replace the fixed 7-agent swarm with a
        # single Brain (Sonnet) driving the 5-phase coordinator loop described
        # in config/prompts/pr_brain_coordinator.md. Brain surveys the PR,
        # plans investigations, dispatches scope-bounded sub-agents via
        # dispatch_subagent, replans on unexpected observations, and
        # synthesises with unified severity classification.
        import os as _os_v2
        if _os_v2.environ.get("CONDUCTOR_PR_BRAIN_V2", "0") == "1":
            async for event in self._run_v2_coordinator(
                pr_context, risk_profile, file_diffs, impact_context,
                budget_multiplier, start_time,
            ):
                yield event
            return

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

        # Collect token usage AND files actually opened by review agents.
        # files_reviewed is the union of (a) files changed in the PR diff and
        # (b) every file any dispatched review agent opened via read_file /
        # file_outline / compressed_view. Required by the eval scorer's
        # context_depth metric so cross-file investigation gets credit.
        total_tokens = 0
        total_iterations = 0
        files_reviewed_set: set[str] = {f.path for f in pr_context.files}
        for result in agent_results:
            if result.success and isinstance(result.data, dict):
                total_iterations += result.data.get("iterations", 0)
                total_tokens += result.data.get("total_input_tokens", 0)
                total_tokens += result.data.get("total_output_tokens", 0)
                for fp in result.data.get("files_accessed", []):
                    if fp:
                        files_reviewed_set.add(fp)

        yield WorkflowEvent(
            "done",
            {
                "answer": synthesis or pr_summary,
                "findings": [_finding_to_dict(f) for f in findings],
                "files_reviewed": sorted(files_reviewed_set),
                "merge_recommendation": merge_rec,
                "duration_ms": duration_ms,
                "total_iterations": total_iterations,
                "agents_dispatched": len(agents_to_run),
                "findings_before_arbitration": len(findings)
                + len([f for f in findings if f.severity == Severity.PRAISE]),
            },
        )

    # ------------------------------------------------------------------
    # PR Brain v2 — coordinator loop
    # ------------------------------------------------------------------

    async def _run_v2_coordinator(
        self,
        pr_context: PRContext,
        risk_profile: RiskProfile,
        file_diffs: Dict[str, str],
        impact_context: str,
        budget_multiplier: float,
        start_time: float,
    ) -> AsyncGenerator[WorkflowEvent, None]:
        """Brain-as-coordinator loop for PR Brain v2.

        Instead of dispatching 7 fixed-role agents in parallel, we spawn ONE
        Brain (Sonnet) with:
          * system prompt = pr_brain_coordinator skill (the 5-phase loop +
            3-check contract + severity rubric)
          * tools = read-only survey tools + ``dispatch_subagent`` (the
            v2 primitive that runs scope-bounded workers returning
            severity-null findings)
          * user message = diff + impact_context + risk profile
        The Brain plans investigations, dispatches workers, replans, and
        emits a structured review directly as its final answer. We parse
        findings from the Brain's output and drop into the same
        post-processing / arbitration / synthesis phases the v1 path uses.

        Gated on CONDUCTOR_PR_BRAIN_V2=1. v1 path remains untouched when
        the flag is off — rollback is a single env var flip.
        """
        from app.workflow.loader import load_brain_config, load_swarm_registry

        from .brain import AgentToolExecutor, BrainBudgetManager
        from .config import BrainExecutorConfig

        logger.info(
            "[PR Brain v2] Coordinator loop starting: files=%d, lines=%d, budget=%.1fx",
            pr_context.file_count,
            pr_context.total_changed_lines,
            budget_multiplier,
        )

        yield WorkflowEvent(
            "v2_coordinator_start",
            {
                "mode": "pr_brain_v2",
                "file_count": pr_context.file_count,
            },
        )

        brain_config = load_brain_config()
        swarm_registry = load_swarm_registry()
        budget_mgr = BrainBudgetManager(
            self._config.limits.total_session_tokens,
        )
        llm_semaphore = asyncio.Semaphore(self._config.limits.llm_concurrency_limit)

        executor_cfg = BrainExecutorConfig(
            workspace_path=self._workspace_path,
            current_depth=0,
            max_depth=self._config.limits.max_depth,
            max_concurrent=self._config.limits.max_concurrent_agents,
            sub_agent_timeout=self._config.limits.sub_agent_timeout,
        )

        executor = AgentToolExecutor(
            inner_executor=self._tool_executor,
            agent_registry=self._agent_registry,
            swarm_registry=swarm_registry,
            agent_provider=self._explorer_provider,  # haiku for sub-agents
            strong_provider=self._provider,          # sonnet = the Brain itself
            config=executor_cfg,
            brain_config=brain_config,
            trace_writer=self._trace_writer,
            event_sink=self._event_sink,
            budget_manager=budget_mgr,
            llm_semaphore=llm_semaphore,
        )

        # ------------------------------------------------------------------
        # Phase 2 — Verify (existence-check sub-agent).
        #
        # Before planning any logic investigations, we dispatch ONE
        # mechanical worker whose job is to verify that every symbol the
        # diff newly references actually exists in the codebase. Its
        # output becomes authoritative existence_facts in the vault;
        # missing symbols short-circuit into "ImportError at runtime"
        # findings without needing a logic-check dispatch.
        #
        # Skipped when CONDUCTOR_PR_BRAIN_V2_SKIP_EXISTENCE=1 for
        # fallback / A-B test scenarios.
        # ------------------------------------------------------------------
        existence_summary = ""
        import os as _os_v2phase2
        if _os_v2phase2.environ.get("CONDUCTOR_PR_BRAIN_V2_SKIP_EXISTENCE", "0") != "1":
            try:
                async for ev in self._run_v2_phase2_existence(
                    executor, pr_context, file_diffs,
                ):
                    yield ev
                existence_summary = self._format_existence_summary_for_coordinator()
            except Exception as exc:
                logger.warning(
                    "[PR Brain v2] Phase 2 existence check failed (non-fatal): %s", exc,
                )

        # Build the coordinator's task — diff + impact + coordinator skill.
        coordinator_query = self._build_v2_coordinator_query(
            pr_context, risk_profile, file_diffs, impact_context,
            existence_summary=existence_summary,
        )

        # Dispatch the Brain itself via dynamic-compose. It gets a tool pool
        # including dispatch_subagent, read-only survey tools, and runs the
        # 5-phase loop under the pr_brain_coordinator skill's direction.
        coordinator_tools = [
            "grep", "read_file", "find_symbol", "file_outline",
            "get_callers", "get_callees", "get_dependencies",
            "git_diff", "git_diff_files", "git_show", "git_log",
            "dispatch_subagent",
        ]

        coordinator_params = {
            "perspective": (
                "You are the PR Brain coordinator. You survey the diff, "
                "plan focused investigations, dispatch scope-bounded "
                "sub-agents via dispatch_subagent, and synthesize the "
                "final review. You classify severity yourself using the "
                "2-question rubric (provable? + blast radius?)."
            ),
            "skill": "pr_brain_coordinator",
            "tools": coordinator_tools,
            "model": "strong",
            # Bumped from 25 → 32 iterations and 400K → 550K tokens to
            # accommodate multi-role-per-cluster dispatch (up to 5 roles
            # × up to 5 clusters on large PRs). Each dispatch consumes
            # ~1 iteration of the coordinator loop; large PRs can now
            # realistically plan 12-16 dispatches without starving the
            # Survey + Synthesize phases.
            "max_iterations": int(32 * budget_multiplier),
            "budget_tokens": int(550_000 * budget_multiplier),
            "query": coordinator_query,
            "budget_weight": 1.0,
        }

        coordinator_result = await executor.execute(
            "dispatch_agent", coordinator_params,
        )

        logger.info(
            "[PR Brain v2] Coordinator loop done: success=%s",
            coordinator_result.success,
        )

        # Parse the coordinator's final answer into ReviewFindings + synthesis.
        review_output = self._parse_v2_coordinator_output(
            coordinator_result, pr_context,
        )

        # ------------------------------------------------------------------
        # Phase 6 — Precision filter with adaptive verifier.
        #
        # Split findings by confidence into 3 bands:
        #   * >= 0.8 : direct final finding
        #   * 0.5-0.8: dispatch verifier(s) (Haiku x N if <=2, Sonnet batch if >=3)
        #              — verifier verdict is terminal
        #   * < 0.5  : secondary_notes (not in findings array; appended to
        #              synthesis text)
        #
        # Skip via env CONDUCTOR_PR_BRAIN_V2_SKIP_VERIFY=1 for A/B testing.
        # ------------------------------------------------------------------
        import os as _os_v2phase6
        if (
            _os_v2phase6.environ.get("CONDUCTOR_PR_BRAIN_V2_SKIP_VERIFY", "0") != "1"
            and review_output["findings"]
        ):
            try:
                review_output = await self._apply_v2_precision_filter(
                    executor, review_output, pr_context, file_diffs,
                )
            except Exception as exc:
                logger.warning(
                    "[PR Brain v2] Precision filter failed (non-fatal): %s", exc,
                )

        yield WorkflowEvent(
            "v2_coordinator_complete",
            {
                "finding_count": len(review_output["findings"]),
            },
        )

        # Files reviewed = PR diff files ∪ everything any subagent touched
        files_reviewed_set: set[str] = {f.path for f in pr_context.files}
        if coordinator_result.success and isinstance(coordinator_result.data, dict):
            for fp in coordinator_result.data.get("files_accessed", []):
                if fp:
                    files_reviewed_set.add(fp)

        duration_ms = (time.monotonic() - start_time) * 1000.0

        yield WorkflowEvent(
            "done",
            {
                "answer": review_output["synthesis"],
                "findings": review_output["findings"],
                "files_reviewed": sorted(files_reviewed_set),
                "merge_recommendation": review_output["merge_recommendation"],
                "duration_ms": duration_ms,
                "total_iterations": coordinator_result.data.get("iterations", 0)
                if isinstance(coordinator_result.data, dict) else 0,
                "agents_dispatched": 1,  # the coordinator itself, sub-dispatches tracked separately
                "findings_before_arbitration": len(review_output["findings"]),
                "mode": "pr_brain_v2",
            },
        )

    async def _run_v2_phase2_existence(
        self,
        executor,
        pr_context: PRContext,
        file_diffs: Dict[str, str],
    ):
        """Dispatch ONE pr_existence_check worker. Its JSON output is
        parsed and persisted to the Fact Vault's ``existence_facts`` table
        so the coordinator (and later sub-agents) can query via
        ``search_facts(kind="existence")``.

        Yielding WorkflowEvent for observability.
        """
        yield WorkflowEvent(
            "v2_phase2_start", {"phase": "existence_verification"},
        )

        # Pack the diff text the worker needs to inspect. Keep bounded so
        # the worker doesn't drown in bytes.
        diff_block: List[str] = []
        remaining = 20_000
        for path, diff_text in file_diffs.items():
            if remaining <= 0:
                diff_block.append(f"[...additional diffs truncated — use git_diff for {path}...]")
                break
            slice_ = diff_text[:remaining]
            diff_block.append(f"### {path}\n```diff\n{slice_}\n```")
            remaining -= len(slice_)

        task_text = (
            "For every NEW symbol this PR references on `+` diff lines "
            "(imports, classes, methods, attributes, decorators), verify "
            "whether it exists in the codebase. Use grep / find_symbol / "
            "read_file. Emit the JSON schema from your system prompt as "
            "your final message."
        )

        # P9 — per-language verification hint. Only injected when the diff
        # touches that language, so a Go-only PR doesn't pay for Java
        # prompt tokens. All four mainstream languages prefer
        # `find_symbol` over grep because tree-sitter handles overloads,
        # receivers, MRO, and nested definitions that signature grep
        # patterns can miss.
        lang_hints: List[str] = []
        extensions = {
            Path(f.path).suffix.lower() for f in pr_context.files if f.path
        }
        if ".java" in extensions:
            lang_hints.append(
                "**Java (`.java`)** — prefer `find_symbol(name)` over grep. "
                "The tree-sitter index enumerates classes, interfaces, "
                "methods, and fields (including overloads). For method "
                "calls with new argument shapes, inspect all overloads "
                "returned by `find_symbol` before flagging as missing — "
                "Java allows same-name methods with different parameter "
                "types. Only fall back to grep when `find_symbol` is "
                "empty AND the file isn't marked `extracted_via: regex`."
            )
        if ".py" in extensions:
            lang_hints.append(
                "**Python (`.py`)** — prefer `find_symbol(name)` over grep "
                "when verifying class methods, `__init__` parameters, or "
                "attributes. AST surfaces inherited methods via MRO and "
                "decorator-wrapped definitions that grep can miss. Grep "
                "on `class Name` / `def name` is acceptable only for "
                "top-level module symbols."
            )
        if ".go" in extensions:
            lang_hints.append(
                "**Go (`.go`)** — prefer `find_symbol(name)` over grep "
                "when checking method receivers (`func (r *R) Name`) or "
                "interface members. AST binds the method to its receiver "
                "type, which grep can't disambiguate across files. Grep "
                "on `func Name` / `type Name struct` is fine for free "
                "functions and simple types."
            )
        if extensions & {".ts", ".tsx", ".js", ".jsx"}:
            lang_hints.append(
                "**TypeScript / JavaScript (`.ts` / `.tsx` / `.js` / "
                "`.jsx`)** — prefer `find_symbol(name)` over grep. AST "
                "reliably picks up function overloads, interface members, "
                "class methods, and type aliases that grep conflates. For "
                "TS overloaded functions, inspect the full signature list "
                "returned by `find_symbol` before flagging a kwarg or "
                "param as missing."
            )

        hint_block = ""
        if lang_hints:
            hint_block = "\n\n## Language-specific hints\n\n" + "\n\n".join(lang_hints)

        query = (
            "# PR existence verification\n\n"
            + task_text
            + hint_block
            + "\n\n## Files changed\n\n"
            + "\n".join(f"- `{f.path}` (+{f.additions} −{f.deletions})" for f in pr_context.files)
            + "\n\n## Diff\n\n"
            + "\n".join(diff_block)
        )

        params = {
            "template": "pr_existence_check",
            "query": query,
            "budget_weight": 0.5,
        }
        # Track per-path status so we can ALWAYS run the P13 deterministic
        # fallback at the end — even if the LLM worker times out or fails
        # to parse. The LLM worker catches signature mismatches we cannot
        # do mechanically; P13 catches the import-level phantom cases
        # regardless. Together they form a belt-and-suspenders pair.
        llm_symbols: List[Dict[str, Any]] = []
        llm_error: Optional[str] = None
        llm_timeout: bool = False

        # Hard orchestrator-level wall-clock cap. Phase 2 is an
        # optimization, not a correctness gate — if the worker hangs on a
        # large codebase despite its prompt-level budget, we'd rather skip
        # existence facts than stall the entire review for 10 minutes.
        try:
            result = await asyncio.wait_for(
                executor.execute("dispatch_agent", params),
                timeout=float(_PHASE2_TIMEOUT_SECONDS),
            )
        except TimeoutError:
            logger.warning(
                "[PR Brain v2] existence-check hit %ds wall-clock timeout; "
                "P13 AST fallback will still run.",
                _PHASE2_TIMEOUT_SECONDS,
            )
            llm_timeout = True
            result = None

        if result is not None and not result.success:
            logger.warning(
                "[PR Brain v2] existence-check dispatch failed: %s", result.error,
            )
            llm_error = str(result.error)
            result = None

        if result is not None:
            condensed = result.data or {}
            raw_answer = (
                condensed.get("answer") or condensed.get("final_answer") or ""
            )
            parsed = _parse_existence_json(raw_answer)
            if parsed is None:
                logger.warning(
                    "[PR Brain v2] existence worker output did not parse as JSON",
                )
                llm_error = "parse_failed"
            else:
                llm_symbols = parsed.get("symbols", []) or []

        # Persist LLM symbols into the vault so later sub-agents can query
        # via search_facts(kind="existence").
        from app.scratchpad import current_factstore

        store = current_factstore()
        missing_count = 0
        if store is not None and llm_symbols:
            for sym in llm_symbols:
                if not isinstance(sym, dict):
                    continue
                name = sym.get("name") or ""
                if not name:
                    continue
                exists = bool(sym.get("exists", True))
                if not exists:
                    missing_count += 1
                try:
                    store.put_existence(
                        symbol_name=name,
                        symbol_kind=(sym.get("kind") or "symbol")[:32],
                        referenced_at=(sym.get("referenced_at") or "")[:256],
                        exists=exists,
                        evidence=(sym.get("evidence") or "")[:1000],
                        signature_info=sym.get("signature_info"),
                    )
                except Exception as exc:
                    logger.debug("put_existence failed for %s: %s", name, exc)

        # P13 — Deterministic Python import verifier (belt-and-suspenders).
        # Runs ALWAYS, regardless of LLM worker timeout / failure / empty
        # output. Zero LLM cost; narrowly scoped to new Python imports
        # so never overrides an LLM exists=True verdict.
        added_from_ast = 0
        if store is not None:
            try:
                already_named = {
                    sym.get("name") for sym in llm_symbols if isinstance(sym, dict)
                }
                for found in _scan_new_python_imports_for_missing(
                    self._workspace_path, file_diffs,
                ):
                    name = found["name"]
                    if name in already_named:
                        continue
                    try:
                        store.put_existence(
                            symbol_name=name,
                            symbol_kind="import",
                            referenced_at=found["referenced_at"],
                            exists=False,
                            evidence=found["evidence"],
                            signature_info=None,
                        )
                        already_named.add(name)
                        added_from_ast += 1
                        missing_count += 1
                    except Exception as exc:
                        logger.debug(
                            "[PR Brain v2] P13 put_existence failed for %s: %s",
                            name, exc,
                        )
            except Exception as exc:
                logger.warning(
                    "[PR Brain v2] P13 deterministic import scan failed "
                    "(non-fatal): %s", exc,
                )
        if added_from_ast:
            logger.info(
                "[PR Brain v2] P13 deterministic import scan found %d "
                "missing symbol(s) the LLM worker did not flag",
                added_from_ast,
            )

        logger.info(
            "[PR Brain v2] Phase 2 existence: %d symbols checked, %d missing "
            "(+%d from AST scan, llm_timeout=%s, llm_error=%s)",
            len(llm_symbols), missing_count, added_from_ast,
            llm_timeout, llm_error,
        )
        yield WorkflowEvent(
            "v2_phase2_complete",
            {
                "phase": "existence_verification",
                "symbols_checked": len(llm_symbols),
                "missing": missing_count,
                "ast_added": added_from_ast,
                "llm_timeout": llm_timeout,
                "llm_error": llm_error,
            },
        )

    def _format_existence_summary_for_coordinator(self) -> str:
        """Render a compact summary of existence_facts the coordinator can
        read inline in its user message. Empty string when no facts.

        Designed to be **hard to ignore** — the coordinator MUST emit each
        missing symbol as a finding in the exact shape shown, not
        speculate about logic in non-existent code.
        """
        from app.scratchpad import current_factstore

        store = current_factstore()
        if store is None:
            return ""

        missing: List = list(store.iter_existence(exists=False))
        present: List = list(store.iter_existence(exists=True))

        if not missing and not present:
            return ""

        lines: List[str] = []
        lines.append("## Phase 2 — Existence verification (AUTHORITATIVE)")
        lines.append("")

        if missing:
            lines.append("### ⚠️ Missing symbols — DIRECT FINDINGS REQUIRED")
            lines.append("")
            lines.append(
                f"The Phase 2 verifier grep/find_symbol'd every new "
                f"reference in this PR. **{len(missing)} symbol(s) are "
                f"NOT defined anywhere in the codebase.** The PR will "
                f"raise `ImportError` / `NameError` / `TypeError` at "
                f"runtime the moment affected code is loaded."
            )
            lines.append("")
            lines.append("**MANDATORY**: your final findings JSON MUST include one "
                         "entry per missing symbol, pointing at the REFERENCE "
                         "site (not where the symbol 'would' be defined), with "
                         "title of the form 'ImportError at runtime: {name} "
                         "not defined in codebase'. Severity = `critical`. "
                         "Category = `correctness`. Confidence = `0.99`.")
            lines.append("")
            lines.append("**DO NOT** speculate about what the non-existent "
                         "symbol 'would have done'. Do NOT emit findings "
                         "about negative offsets, null checks, or any logic "
                         "inside a phantom class. The class does not exist — "
                         "the ImportError IS the bug. Stop there.")
            lines.append("")
            lines.append("**Required finding template** (copy this shape — fill the brackets):")
            lines.append("")
            lines.append("```json")
            lines.append("{")
            lines.append('  "title": "ImportError at runtime: <SYMBOL> not defined in codebase",')
            lines.append('  "severity": "critical",')
            lines.append('  "confidence": 0.99,')
            lines.append('  "file": "<FILE where the reference is>",')
            lines.append('  "start_line": <LINE of the reference>,')
            lines.append('  "end_line": <LINE of the reference>,')
            lines.append('  "evidence": ["grep \'class <SYMBOL>\' / \'def <SYMBOL>\' returned 0 matches in the codebase"],')
            lines.append('  "risk": "Every call path that loads <FILE> raises ImportError/NameError at runtime.",')
            lines.append('  "suggested_fix": "Either define <SYMBOL> in the imported module, or remove the reference. The current PR is unshippable as written.",')
            lines.append('  "category": "correctness"')
            lines.append("}")
            lines.append("```")
            lines.append("")
            lines.append("**Missing symbols (one finding each — do not merge, do not skip):**")
            lines.append("")
            for m in missing:
                ref = m.referenced_at or "(unknown)"
                ev = (m.evidence or "").strip()[:200]
                lines.append(
                    f"- `{m.symbol_name}` ({m.symbol_kind}) referenced at `{ref}` — evidence: {ev}"
                )
            lines.append("")

        if present:
            sig_mismatch = [
                p for p in present
                if p.signature_info and p.signature_info.get("missing_params")
            ]
            if sig_mismatch:
                lines.append("### ⚠️ Signature mismatches — DIRECT FINDINGS REQUIRED")
                lines.append("")
                lines.append(
                    f"**{len(sig_mismatch)} method(s) exist but are called "
                    f"with parameter(s) they don't accept.** Runtime "
                    f"behaviour: `TypeError: unexpected keyword argument`. "
                    f"Emit one finding each using the same template shape "
                    f"above, but with title 'TypeError at runtime: "
                    f"{{method}}() does not accept {{kwarg}}'."
                )
                lines.append("")
                for m in sig_mismatch:
                    missing_params = m.signature_info.get("missing_params", [])
                    lines.append(
                        f"- `{m.symbol_name}` at `{m.referenced_at}` — "
                        f"missing params: {missing_params}"
                    )
                lines.append("")
            other_present = [p for p in present if p not in sig_mismatch]
            if other_present:
                lines.append(
                    f"**{len(other_present)} other symbol(s) verified present.** "
                    f"Use `search_facts(kind=\"existence\", symbol=\"X\")` to "
                    f"look up any of them; sub-agents you dispatch can "
                    f"skip the verify-existence-first step for these."
                )
                lines.append("")
        return "\n".join(lines)

    def _build_v2_coordinator_query(
        self,
        pr_context: PRContext,
        risk_profile: RiskProfile,
        file_diffs: Dict[str, str],
        impact_context: str,
        existence_summary: str = "",
    ) -> str:
        """Compose the user message for the v2 coordinator Brain.

        Includes: file list with +/- counts, risk profile summary, condensed
        impact context, and the diff itself (truncated per budget). The
        pr_brain_coordinator skill in the system prompt drives the loop.
        """
        lines: List[str] = []
        lines.append("# PR Review — coordinator task")
        lines.append("")
        lines.append(f"Diff spec: `{self._diff_spec}`")
        lines.append(f"Files changed: {pr_context.file_count}  "
                     f"Lines changed: {pr_context.total_changed_lines}")
        lines.append("")

        # ------------------------------------------------------------------
        # PR intent — the single most important seed for Plan phase.
        # Without this, the coordinator can only pattern-match on the diff;
        # with it, the coordinator can derive invariants to check.
        # ------------------------------------------------------------------
        pr_title = getattr(pr_context, "title", "") or ""
        pr_desc = getattr(pr_context, "description", "") or ""
        if pr_title or pr_desc:
            lines.append("## PR intent — what this PR CLAIMS to do")
            lines.append("")
            if pr_title:
                lines.append(f"**Title**: {pr_title}")
                lines.append("")
            if pr_desc:
                lines.append("**Description**:")
                lines.append("")
                lines.append(pr_desc.strip()[:1800])
                if len(pr_desc.strip()) > 1800:
                    lines.append("\n[...description truncated — fetch more with tools if needed...]")
                lines.append("")
            lines.append(
                "**Before planning investigations**: extract 3-5 concrete "
                "invariants from the intent above. Each invariant should be "
                "a falsifiable predicate of the shape 'After this PR, {X} "
                "must hold at {location/type}'. These invariants drive your "
                "dispatch_subagent check questions — every check should map "
                "to one invariant. If an invariant cannot be checked from "
                "the diff alone, grep / find_symbol first to find the "
                "relevant code."
            )
            lines.append("")
            lines.append(
                "**Intent check**: use the intent as context for your "
                "regular investigations. If a concrete code bug already "
                "captures the problem, emit ONE finding about that bug — "
                "do NOT also emit a separate 'intent mismatch' meta-finding "
                "covering the same defect. Only emit a standalone intent "
                "finding when the diff visibly fails to achieve the stated "
                "goal AND no concrete code-level bug explains the gap."
            )
            lines.append("")

        lines.append("## Files in diff")
        lines.append("")
        for f in pr_context.files:
            lines.append(
                f"- `{f.path}`  (+{f.additions} −{f.deletions}, "
                f"{f.status}, category={f.category.value})"
            )
        lines.append("")
        lines.append("## Risk profile")
        lines.append("")
        lines.append(f"- correctness: {risk_profile.correctness.value}")
        lines.append(f"- security: {risk_profile.security.value}")
        lines.append(f"- reliability: {risk_profile.reliability.value}")
        lines.append(f"- concurrency: {risk_profile.concurrency.value}")
        lines.append(f"- operational: {risk_profile.operational.value}")
        lines.append("")

        # Phase 2 output (existence facts) injected inline. Missing
        # symbols here are directly promotable findings — the coordinator
        # should NOT dispatch logic checks on them.
        if existence_summary:
            lines.append(existence_summary)
            lines.append("")

        # Impact context (condensed). Keep it bounded.
        if impact_context:
            lines.append("## Impact context (dependency graph + callers)")
            lines.append("")
            lines.append(impact_context[:8000])
            if len(impact_context) > 8000:
                lines.append("\n[...truncated, use tools to explore further...]")
            lines.append("")

        # File diffs — include but bound size. Full diffs are the primary
        # evidence; coordinator will read files directly for deeper cuts.
        lines.append("## Diff (per-file)")
        lines.append("")
        diff_budget = 30_000  # chars across all diffs
        remaining = diff_budget
        for path, diff_text in file_diffs.items():
            if remaining <= 0:
                lines.append("[...more diffs truncated, use git_diff tool to fetch...]")
                break
            slice_ = diff_text[: min(len(diff_text), remaining)]
            lines.append(f"### `{path}`")
            lines.append("```diff")
            lines.append(slice_)
            lines.append("```")
            lines.append("")
            remaining -= len(slice_)

        # Dispatch cap scales with PR size (your skill covers the "why"
        # in the Plan section; here we give you the numeric cap). Caps
        # bumped in v2o to give multi-role-per-cluster (0-5 roles) real
        # room — a 4-cluster large PR with 2-3 roles per cluster easily
        # wants 10-14 dispatches.
        n_files = len(pr_context.files)
        if n_files < 5:
            dispatch_cap = 5
            size_label = "small"
        elif n_files < 15:
            dispatch_cap = 10
            size_label = "medium"
        else:
            dispatch_cap = 16
            size_label = "large"

        lines.append("## Dispatch budget for THIS PR")
        lines.append("")
        lines.append(
            f"- PR size: **{size_label}** ({n_files} files, "
            f"{pr_context.total_changed_lines} lines changed)"
        )
        lines.append(
            f"- Hard cap: **{dispatch_cap} dispatches** across all replan rounds"
        )
        if size_label == "large":
            lines.append(
                "- Cluster first: group files by feature/intent in Survey, "
                "then dispatch 1-2 role agents per cluster"
            )
        else:
            lines.append(
                "- Small PR: 1-3 targeted dispatches typically suffice. "
                "Don't pad."
            )
        lines.append("")

        lines.append("## Your task")
        lines.append("")
        lines.append(
            "Run your 5-phase coordinator loop (Survey → Plan → Execute → "
            "Replan → Synthesize). Use read-only tools for the Survey. "
            "Dispatch scope-bounded investigations via dispatch_subagent "
            f"(≤5 files per dispatch, ≤{dispatch_cap} total dispatches). "
            "Two dispatch modes available — pick per investigation: "
            "(a) `checks=[q1, q2, q3]` for localised suspicions where "
            "you have concrete yes/no questions; (b) `role=\"security\"|"
            "\"correctness\"|\"concurrency\"|\"reliability\"|\"performance\"|"
            "\"test_coverage\"` + `direction_hint=\"...\"` for specialist "
            "deep-dive on a risk dimension. You may combine: "
            "`role=\"security\", checks=[...]`. "
            "At Synthesize, classify severity yourself using the "
            "`## Severity rubric` section of your skill — reserve `critical` "
            "and `high` for their listed categories, default borderline "
            "findings to `medium`. Write `suggested_fix` in the concrete, "
            "location-bearing shape shown in the `## Suggested_fix` section."
        )
        lines.append("")
        lines.append("## Final output — MANDATORY SHAPE")
        lines.append("")
        lines.append(
            "Your final answer must be a JSON array of findings inside a "
            "```json fenced block. Each finding has these fields:"
        )
        lines.append("")
        lines.append("```json")
        lines.append("[")
        lines.append("  {")
        lines.append('    "title": "concise description",')
        lines.append('    "severity": "critical | high | medium | low | nit | praise",')
        lines.append('    "confidence": 0.0-1.0,')
        lines.append('    "file": "path/to/file.py",')
        lines.append('    "start_line": 120,')
        lines.append('    "end_line": 135,')
        lines.append('    "evidence": ["line quote", "cross-reference"],')
        lines.append('    "risk": "what could go wrong in production",')
        lines.append('    "suggested_fix": "concrete, implementable fix",')
        lines.append('    "category": "correctness | security | reliability | concurrency | performance | test_coverage"')
        lines.append("  }")
        lines.append("]")
        lines.append("```")
        lines.append("")
        lines.append(
            "**Always emit at least one finding.** A reviewer reading your "
            "output expects a signal per PR. If after honest investigation "
            "you do NOT see any correctness/security/reliability bugs, "
            "emit a single `praise` severity entry pointing at the primary "
            "change (or an `info` entry noting what you verified and why "
            "nothing rose above the bar). This keeps downstream tooling "
            "happy and gives the author confidence the review was "
            "substantive. Do NOT invent filler bugs — praise/info on a "
            "clean PR is honest and useful. After the JSON block you may "
            "add a short prose synthesis, but the JSON array is what "
            "downstream tooling parses — it must be present, valid, and "
            "non-empty."
        )
        return "\n".join(lines)

    async def _apply_v2_precision_filter(
        self,
        executor,
        review_output: Dict[str, Any],
        pr_context: PRContext,
        file_diffs: Dict[str, str],
    ) -> Dict[str, Any]:
        """3-band precision filter — adaptive verifier.

        Bands:
          * >= 0.8 : keep as final finding (no re-verification)
          * 0.5-0.8: verify via sub-agent (Haiku x N if count <= 2,
                      Sonnet batch if count >= 3). Verdict is terminal.
          * < 0.5  : demote to secondary_notes appended to synthesis.
        """
        findings = review_output.get("findings", [])

        # Step 0: dedup by (file, line±5). When two findings point at
        # (approximately) the same location, keep the one with highest
        # confidence. Deterministic tiebreak: critical > high > medium >
        # low > nit > praise.
        findings = _dedup_findings_by_location(findings)

        # Step 0b: mechanically enforce "one finding per missing symbol"
        # from Phase 2 existence verification. The coordinator skill
        # marks this MANDATORY, but LLM variance can drop or merge these.
        # Injecting synthetic findings here guarantees the review reports
        # every runtime error the diff introduces.
        findings, injected_count = _inject_missing_symbol_findings(findings)
        if injected_count:
            logger.info(
                "[PR Brain v2] Injected %d missing-symbol finding(s) "
                "that coordinator omitted",
                injected_count,
            )

        # Step 0b-2: P14 — inject findings for stub-function call sites
        # detected mechanically from the diff. For each (stub_def,
        # caller) pair found by _scan_for_stub_call_sites, if the
        # coordinator didn't already flag the site, synthesize a
        # finding. Guards against coordinator missing multi-site stub
        # bugs (grafana-009 class).
        findings, stub_injected = _inject_stub_caller_findings(
            findings, file_diffs,
        )
        if stub_injected:
            logger.info(
                "[PR Brain v2] P14 injected %d stub-call-site finding(s)",
                stub_injected,
            )

        # Step 0c: external-signal reflection (P8). Drop findings whose
        # premise contradicts Phase 2 existence facts (e.g. "X doesn't
        # exist" when Phase 2 confirmed exists=True). External signal >
        # intrinsic self-correction (+18.5pp in published research).
        findings, reflection_drops = _reflect_against_phase2_facts(findings)
        if reflection_drops:
            logger.info(
                "[PR Brain v2] Reflection pass dropped %d finding(s) "
                "whose premise contradicts Phase 2 facts",
                reflection_drops,
            )

        if not findings:
            return review_output

        direct: List[Dict[str, Any]] = []
        unclear: List[Dict[str, Any]] = []
        low: List[Dict[str, Any]] = []

        for f in findings:
            conf = float(f.get("confidence", 0) or 0)
            if conf >= 0.8:
                direct.append(f)
            elif conf >= 0.5:
                unclear.append(f)
            else:
                low.append(f)

        logger.info(
            "[PR Brain v2] Precision filter: direct=%d unclear=%d low=%d",
            len(direct), len(unclear), len(low),
        )

        confirmed_from_verifier: List[Dict[str, Any]] = []
        refuted_count = 0
        unclear_after_verify: List[Dict[str, Any]] = []

        if unclear:
            if len(unclear) <= 2:
                # Haiku per-finding
                for f in unclear:
                    verdict = await self._verify_single(executor, f, file_diffs)
                    if verdict == "confirmed":
                        confirmed_from_verifier.append(f)
                    elif verdict == "refuted":
                        refuted_count += 1
                    else:
                        unclear_after_verify.append(f)
            else:
                # Sonnet batch — amortize context via prompt cache
                results = await self._verify_batch(executor, unclear, file_diffs)
                for f, verdict in zip(unclear, results):
                    if verdict == "confirmed":
                        confirmed_from_verifier.append(f)
                    elif verdict == "refuted":
                        refuted_count += 1
                    else:
                        unclear_after_verify.append(f)

        logger.info(
            "[PR Brain v2] Verifier: confirmed=%d refuted=%d still_unclear=%d",
            len(confirmed_from_verifier), refuted_count, len(unclear_after_verify),
        )

        final_findings = direct + confirmed_from_verifier
        secondary = unclear_after_verify + low

        # Step 6: per-finding diff-scope verification (P11 cheap).
        # Inspired by UltraReview's "every finding independently verified".
        # Mechanical LLM-free check: a finding targeting a file outside
        # the PR diff is almost always a coordinator hallucination. Move
        # such findings to secondary_notes instead of emitting.
        final_findings, scope_demoted, scope_demoted_count = (
            _filter_findings_to_diff_scope(final_findings, file_diffs)
        )
        if scope_demoted_count:
            logger.info(
                "[PR Brain v2] Diff-scope filter demoted %d finding(s) "
                "whose file is not in the PR diff",
                scope_demoted_count,
            )
            secondary = scope_demoted + secondary

        # Append secondary notes to synthesis as a "Secondary observations"
        # block. They don't enter the findings array → don't count against
        # precision / recall in the eval scorer.
        synthesis = review_output.get("synthesis", "")
        if secondary:
            secondary_block_lines = [
                "",
                "---",
                "",
                "## Secondary observations (not scored, low-confidence or "
                "unverified)",
                "",
            ]
            for s in secondary:
                title = s.get("title", "(untitled)")
                file_ = s.get("file", "")
                line = s.get("start_line", "")
                conf = s.get("confidence", "")
                secondary_block_lines.append(
                    f"- **{title}** — `{file_}:{line}` (conf={conf})"
                )
            synthesis = synthesis + "\n".join(secondary_block_lines)

        return {
            **review_output,
            "findings": final_findings,
            "synthesis": synthesis,
            "_precision_filter_stats": {
                "direct_findings": len(direct),
                "unclear_input": len(unclear),
                "confirmed_by_verifier": len(confirmed_from_verifier),
                "refuted_by_verifier": refuted_count,
                "still_unclear": len(unclear_after_verify),
                "low_confidence": len(low),
                "reflection_dropped": reflection_drops,
                "diff_scope_demoted": scope_demoted_count,
            },
        }

    async def _verify_single(
        self, executor, finding: Dict[str, Any], file_diffs: Dict[str, str],
    ) -> str:
        """Dispatch one Haiku verifier on a single finding. Returns verdict
        string: 'confirmed' | 'refuted' | 'unclear'."""
        title = finding.get("title", "")
        file_ = finding.get("file", "")
        start = finding.get("start_line", 0)
        end = finding.get("end_line", 0)
        evidence_hint = finding.get("evidence") or []
        if isinstance(evidence_hint, list):
            evidence_hint = "; ".join(str(e) for e in evidence_hint[:3])

        diff_snippet = file_diffs.get(file_, "")[:4000]

        query = (
            f"# Verify this single finding\n\n"
            f"**Title**: {title}\n"
            f"**File**: {file_}\n"
            f"**Lines**: {start}-{end}\n"
            f"**Original confidence**: {finding.get('confidence', 0)}\n"
            f"**Agent's evidence claim**: {evidence_hint}\n\n"
            f"## File diff (relevant)\n\n"
            f"```diff\n{diff_snippet}\n```\n\n"
            f"Return the JSON verdict from your system prompt."
        )

        result = await executor.execute(
            "dispatch_agent",
            {
                "template": "pr_verification_single",
                "query": query,
                "budget_weight": 0.3,
            },
        )
        if not result.success:
            return "unclear"

        data = result.data or {}
        raw = data.get("answer") or data.get("final_answer") or ""
        verdict = _extract_single_verdict(raw)
        return verdict

    async def _verify_batch(
        self, executor, unclear: List[Dict[str, Any]], file_diffs: Dict[str, str],
    ) -> List[str]:
        """Dispatch one Sonnet verifier on N>=3 findings. Returns list of
        verdict strings, in the same order as input."""
        findings_block_lines: List[str] = []
        for i, f in enumerate(unclear):
            title = f.get("title", "")
            file_ = f.get("file", "")
            start = f.get("start_line", 0)
            end = f.get("end_line", 0)
            conf = f.get("confidence", 0)
            ev_raw = f.get("evidence") or []
            if isinstance(ev_raw, list):
                ev_raw = "; ".join(str(e) for e in ev_raw[:3])
            findings_block_lines.append(
                f"### Finding [{i}]\n"
                f"- Title: {title}\n"
                f"- File: {file_}:{start}-{end}\n"
                f"- Original confidence: {conf}\n"
                f"- Agent's evidence claim: {ev_raw}\n"
            )

        touched_files = {f.get("file", "") for f in unclear} - {""}
        diff_snippets: List[str] = []
        for path in sorted(touched_files):
            snippet = file_diffs.get(path, "")[:3000]
            if snippet:
                diff_snippets.append(f"### `{path}` diff\n\n```diff\n{snippet}\n```")

        query = (
            "# Verify these findings in batch\n\n"
            "For each finding, return confirmed|refuted|unclear with "
            "file:line evidence. Cross-reference allowed.\n\n"
            + "\n".join(findings_block_lines)
            + "\n\n## Diffs\n\n"
            + "\n".join(diff_snippets)
            + "\n\nReturn the JSON verdicts object from your system prompt."
        )

        result = await executor.execute(
            "dispatch_agent",
            {
                "template": "pr_verification_batch",
                "query": query,
                "budget_weight": 1.0,
            },
        )
        if not result.success:
            return ["unclear"] * len(unclear)

        data = result.data or {}
        raw = data.get("answer") or data.get("final_answer") or ""
        verdicts = _extract_batch_verdicts(raw, expected_count=len(unclear))
        return verdicts

    def _parse_v2_coordinator_output(
        self,
        coordinator_result,
        pr_context: PRContext,
    ) -> Dict[str, Any]:
        """Extract findings + merge recommendation from the v2 coordinator's
        final Markdown answer.

        Uses the existing ``parse_findings`` + ``merge_recommendation``
        helpers from ``code_review.shared`` so the output shape matches
        v1's. If the coordinator's answer can't be parsed, falls back to
        returning the raw answer as synthesis with zero findings — the
        agent still produced SOMETHING, no reason to hide it.
        """
        from app.code_review.shared import (
            merge_recommendation as _merge_rec,
        )
        from app.code_review.shared import (
            parse_findings as _parse_findings,
        )

        default = {
            "findings": [],
            "synthesis": "",
            "merge_recommendation": "comment",
        }

        if not coordinator_result.success:
            err = getattr(coordinator_result, "error", "unknown error")
            default["synthesis"] = (
                f"PR Brain v2 coordinator failed: {err}"
            )
            return default

        data = coordinator_result.data
        if not isinstance(data, dict):
            return default

        raw_answer = data.get("answer") or data.get("final_answer") or ""

        from app.code_review.models import FindingCategory as _FC

        try:
            # parse_findings accepts a default category and will override per
            # finding when the LLM included a "Category:" marker in its block.
            review_findings = _parse_findings(
                raw_answer,
                agent_name="pr_brain_v2",
                category=_FC.CORRECTNESS,
                warn_on_empty=False,
            )
        except Exception as exc:
            logger.warning(
                "[PR Brain v2] Failed to parse coordinator output: %s. "
                "Returning raw answer as synthesis with 0 findings.",
                exc,
            )
            return {
                "findings": [],
                "synthesis": raw_answer or default["synthesis"],
                "merge_recommendation": "comment",
            }

        try:
            merge_rec = _merge_rec(review_findings)
        except Exception:
            merge_rec = "comment"

        findings_dicts = [_finding_to_dict(f) for f in review_findings]
        return {
            "findings": findings_dicts,
            "synthesis": raw_answer,
            "merge_recommendation": merge_rec or "comment",
        }

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

    def cleanup(self) -> None:
        """Close and delete the session-owned Fact Vault, if any.

        Must be called once the orchestrator is done (success OR failure).
        Callers that passed a vault via ``scratchpad=`` keep ownership —
        we only delete what we created ourselves. Safe to call multiple
        times; second call is a no-op.

        Also resets the ContextVar binding so ``search_facts`` in any
        other concurrent task stops pointing at our (now-deleted) DB.
        """
        # Reset the ContextVar binding regardless of ownership — if we
        # set it, we reset it, so concurrent search_facts calls won't hit
        # a deleted store.
        if self._scratchpad_ctx_token is not None:
            try:
                from app.scratchpad.context import _current_store

                _current_store.reset(self._scratchpad_ctx_token)
            except (LookupError, ValueError) as e:
                # Token already reset or context mismatch; safe to ignore.
                logger.debug("Scratchpad ContextVar reset skipped: %s", e)
            self._scratchpad_ctx_token = None

        if not self._owns_scratchpad or self._scratchpad is None:
            return
        try:
            stats = self._scratchpad.stats()
            exec_stats = getattr(self._tool_executor, "stats", None)
            # WARNING level so the line lands in default-level loggers
            # (root level is WARNING). One emit per PR review — low noise,
            # high signal: hits / misses / range_hits / negative_hits /
            # skipped from CachedToolExecutor + facts/negative_facts/
            # skip_facts counts from FactStore. Critical observability for
            # the eval harness.
            logger.warning(
                "Scratchpad close: session=%s stats=%s cache_perf=%s",
                self._scratchpad.session_id,
                stats,
                exec_stats,
            )
            self._scratchpad.delete()
        except Exception as e:
            logger.warning("Scratchpad cleanup failed: %s", e)
        self._scratchpad = None
        self._owns_scratchpad = False

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
        # Scope files per agent type (9.12 diff sharding — keep per-agent input
        # token count low; security widens to path-pattern-matched auth/crypto/
        # session files so it sees helpers classified outside business_logic).
        files = pr_context.business_logic_files()
        if agent_name == "test_coverage":
            files = pr_context.files
        elif agent_name == "security":
            # Deduplicate while preserving order — a file can match multiple
            # scoping rules (e.g. auth_service.py is both business_logic AND
            # security_sensitive).
            seen: set = set()
            combined = []
            for f in (
                pr_context.business_logic_files()
                + pr_context.config_files()
                + pr_context.security_sensitive_files()
            ):
                if f.path not in seen:
                    seen.add(f.path)
                    combined.append(f)
            files = combined

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

        # PR intent block — only shown when caller supplied title/description.
        # Format: smart-colleague briefing. Agents read this to know what the
        # PR SHOULD do, then check whether the diff actually achieves it.
        intent_block = ""
        pr_title = getattr(pr_context, "title", "") or ""
        pr_desc = getattr(pr_context, "description", "") or ""
        if pr_title or pr_desc:
            intent_parts = ["<pr_intent>"]
            if pr_title:
                intent_parts.append(f"Title: {pr_title}")
            if pr_desc:
                # Bound description to keep token count controlled.
                desc_snippet = pr_desc.strip()[:1500]
                intent_parts.append(f"Description: {desc_snippet}")
                if len(pr_desc.strip()) > 1500:
                    intent_parts.append("[...description truncated...]")
            intent_parts.append("</pr_intent>")
            intent_parts.append(
                "\nUse the intent above to judge whether the diff delivers "
                "what the PR claims. A correctness defect includes: "
                "(1) wrong code logic, AND (2) code that doesn't fulfil "
                "the stated PR goal."
            )
            intent_block = "\n".join(intent_parts) + "\n\n"

        return f"""\
Review this PR for {agent_name.replace("_", " ")} issues.

## Your Focus
{focus}

## Investigation Strategy
{strategy}

{intent_block}<pr_context>
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

            # Parse JSON findings from agent answer. The boolean signals
            # whether the agent emitted an explicit JSON array (even an
            # empty []), which is the authoritative "no findings" answer
            # — we must NOT trigger repair in that case.
            findings, parsed_explicit_array = parse_findings_with_status(
                answer, agent_name, category
            )

            # Repair fallback: only when parse genuinely failed (no array
            # was emitted) AND the answer has substance (>100 chars). This
            # catches truncated outputs from FORCE_CONCLUDE — the agent ran
            # out of budget mid-investigation but still has evidence in its
            # accumulated text. Skipping repair on legitimate empty answers
            # avoids ~1K wasted tokens per agent that has nothing to report.
            if not findings and not parsed_explicit_array and len(answer) > 100:
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


_SEVERITY_RANK = {
    "critical": 5, "high": 4, "medium": 3,
    "low": 2, "nit": 1, "praise": 0,
}


def _dedup_findings_by_location(findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge findings pointing at the same (file, line±5) range.

    Keeps the finding with the highest (severity_rank, confidence) tuple.
    This catches the "coordinator produces the concrete bug finding PLUS
    a meta-finding about it" duplication observed on requests-012.
    """
    if not findings:
        return findings

    keep: List[Dict[str, Any]] = []

    def _better(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
        sev_a = _SEVERITY_RANK.get(str(a.get("severity", "low")).lower(), 1)
        sev_b = _SEVERITY_RANK.get(str(b.get("severity", "low")).lower(), 1)
        if sev_a != sev_b:
            return sev_a > sev_b
        return float(a.get("confidence", 0) or 0) > float(b.get("confidence", 0) or 0)

    for f in findings:
        file_ = f.get("file", "") or ""
        start = int(f.get("start_line", 0) or 0)
        end = int(f.get("end_line", 0) or start or 0)

        merged = False
        for i, existing in enumerate(keep):
            ef = existing.get("file", "") or ""
            if ef != file_:
                continue
            es = int(existing.get("start_line", 0) or 0)
            ee = int(existing.get("end_line", 0) or es or 0)
            # Overlap or adjacency within 5 lines
            if start <= ee + 5 and end >= es - 5:
                # Same region — keep the stronger one
                if _better(f, existing):
                    keep[i] = f
                merged = True
                break
        if not merged:
            keep.append(f)

    return keep


def _finding_covers_symbol(
    finding: Dict[str, Any], symbol_name: str, reference_file: str,
) -> bool:
    """True if ``finding`` already reports the missing-symbol bug for
    ``symbol_name``. Matching rules — ANY one is enough:
      * title contains the symbol name (case-sensitive: class/method
        names are meaningful identifiers)
      * any evidence entry mentions the symbol name
      * the finding's file matches the reference site AND the title
        signals a runtime error (ImportError/NameError/TypeError/
        undefined/not defined)
    """
    if not symbol_name:
        return True  # nothing to enforce

    title = str(finding.get("title", "") or "")
    if symbol_name in title:
        return True

    evidence = finding.get("evidence") or []
    if isinstance(evidence, list):
        for e in evidence:
            if symbol_name in str(e):
                return True
    elif isinstance(evidence, str) and symbol_name in evidence:
        return True

    # Fallback: same file + runtime-error title phrasing.
    f_file = str(finding.get("file", "") or "")
    ref_file = reference_file.split(":", 1)[0] if reference_file else ""
    if f_file and ref_file and f_file == ref_file:
        lowered = title.lower()
        for marker in (
            "importerror", "nameerror", "typeerror",
            "undefined", "not defined", "does not exist",
            "missing symbol",
        ):
            if marker in lowered:
                return True
    return False


def _parse_reference_location(ref: str) -> tuple[str, int]:
    """Split ``"path/to/file.py:42"`` → ``("path/to/file.py", 42)``.
    Falls back to ``(ref, 0)`` when no colon or unparsable line number."""
    if not ref:
        return ("", 0)
    if ":" not in ref:
        return (ref, 0)
    path, _, tail = ref.rpartition(":")
    try:
        return (path, int(tail.strip()))
    except (ValueError, TypeError):
        return (ref, 0)


def _inject_missing_symbol_findings(
    findings: List[Dict[str, Any]],
) -> tuple[List[Dict[str, Any]], int]:
    """Ensure every Phase-2 missing symbol AND signature mismatch has a
    finding in the review.

    Two classes of enforcement:
      * ``exists=False`` — symbol referenced but never defined anywhere.
        Synthesize an ImportError/NameError finding at the reference site.
      * ``exists=True`` with ``signature_info.missing_params`` — method
        is called with kwargs it doesn't accept. Synthesize a TypeError
        finding at the call site.

    Returns ``(findings_with_injections, injected_count)``. Safe to call
    when no FactStore is active — returns the input unchanged.
    """
    from app.scratchpad import current_factstore

    store = current_factstore()
    if store is None:
        return (findings, 0)

    try:
        missing = list(store.iter_existence(exists=False))
        present = list(store.iter_existence(exists=True))
    except Exception as exc:
        logger.warning(
            "[PR Brain v2] missing-symbol post-pass skipped — "
            "iter_existence failed: %s", exc,
        )
        return (findings, 0)

    sig_mismatches = [
        p for p in present
        if p.signature_info
        and p.signature_info.get("missing_params")
    ]

    if not missing and not sig_mismatches:
        return (findings, 0)

    injected = 0
    result = list(findings)

    for m in missing:
        if any(
            _finding_covers_symbol(f, m.symbol_name, m.referenced_at or "")
            for f in result
        ):
            continue
        ref_file, ref_line = _parse_reference_location(m.referenced_at or "")
        evidence_detail = (m.evidence or "").strip()[:300]
        synthetic = {
            "title": (
                f"ImportError at runtime: {m.symbol_name} "
                f"not defined in codebase"
            ),
            "severity": "critical",
            "confidence": 0.99,
            "file": ref_file,
            "start_line": ref_line,
            "end_line": ref_line,
            "evidence": [
                f"Phase 2 verifier: no definition found for `{m.symbol_name}` "
                f"({m.symbol_kind}) anywhere in the workspace.",
                evidence_detail or "grep/find_symbol returned 0 matches.",
            ],
            "risk": (
                f"Every call path that loads `{ref_file}` raises "
                f"ImportError/NameError at runtime — the PR is unshippable "
                f"as-is."
            ),
            "suggested_fix": (
                f"Either define `{m.symbol_name}` in the imported module, "
                f"or remove the reference at {m.referenced_at or ref_file}."
            ),
            "category": "correctness",
            "_injected_from": "phase2_existence_missing",
        }
        result.append(synthetic)
        injected += 1

    for p in sig_mismatches:
        bad_params = p.signature_info.get("missing_params") or []
        if not bad_params:
            continue
        bad_list = [str(bp) for bp in bad_params]
        # Check each bad-param name against existing findings — skip if
        # any kwarg is already covered.
        if any(
            any(_finding_covers_symbol(f, bp, p.referenced_at or "")
                for f in result)
            for bp in bad_list
        ):
            continue
        ref_file, ref_line = _parse_reference_location(p.referenced_at or "")
        accepted = p.signature_info.get("actual_params") or []
        synthetic = {
            "title": (
                f"TypeError at runtime: {p.symbol_name}() does not accept "
                f"{', '.join(bad_list)}"
            ),
            "severity": "high",
            "confidence": 0.97,
            "file": ref_file,
            "start_line": ref_line,
            "end_line": ref_line,
            "evidence": [
                f"Phase 2 verifier: `{p.symbol_name}` signature accepts "
                f"{accepted}; this call passes {bad_list} which are not in "
                f"the signature.",
            ],
            "risk": (
                f"Every invocation raises `TypeError: unexpected keyword "
                f"argument '{bad_list[0]}'` at runtime."
            ),
            "suggested_fix": (
                f"Either extend `{p.symbol_name}`'s signature to accept "
                f"{bad_list}, or drop the unsupported kwarg(s) from the "
                f"call at {p.referenced_at or ref_file}."
            ),
            "category": "correctness",
            "_injected_from": "phase2_existence_sigmismatch",
        }
        result.append(synthetic)
        injected += 1

    return (result, injected)


_PYTHON_FROM_IMPORT_RE = re.compile(
    r"^\+\s*from\s+([.\w]+)\s+import\s+(.+?)\s*$",
)
_PYTHON_BARE_IMPORT_RE = re.compile(
    r"^\+\s*import\s+([\w.]+)(?:\s+as\s+(\w+))?\s*$",
)
_DIFF_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)")


def _scan_new_python_imports_for_missing(
    workspace_path: str,
    file_diffs: Dict[str, str],
    *,
    max_symbols_checked: int = 24,
    grep_timeout_s: float = 8.0,
) -> List[Dict[str, str]]:
    """P13 — Deterministic Python import verifier.

    Scans each Python file's unified diff for newly added imports
    (``+from X import Y`` or ``+import X``) and verifies each imported
    name is defined somewhere in the workspace via a mechanical grep
    for ``class Y`` / ``def Y`` / ``Y = ...``. Returns the list of
    UNDEFINED names, each as ``{"name", "referenced_at", "evidence"}``.

    This is a safety net against the LLM Phase 2 worker missing a
    phantom symbol. Runs always; cheap; Python-only.

    Guards:
      * caps at ``max_symbols_checked`` greps per PR to bound runtime on
        large diffs
      * ``grep_timeout_s`` on each subprocess (so a giant repo cannot
        wedge the review)
      * skips wildcard (``*``), relative (``from .foo import``), and
        framework (``os/re/typing/logging/django/...``) imports
      * fails soft — any exception just returns current findings
    """
    if not workspace_path or not file_diffs:
        return []

    found: List[Dict[str, str]] = []
    checked = 0
    seen_names: set = set()

    for file_path, diff_text in file_diffs.items():
        if not file_path.endswith(".py"):
            continue
        current_new_line = 0
        for raw in diff_text.splitlines():
            if raw.startswith("@@"):
                m = _DIFF_HUNK_HEADER_RE.match(raw)
                if m:
                    current_new_line = int(m.group(1))
                continue
            if raw.startswith("---") or raw.startswith("+++"):
                continue
            is_addition = raw.startswith("+")
            if is_addition:
                from_match = _PYTHON_FROM_IMPORT_RE.match(raw)
                if from_match:
                    module = from_match.group(1)
                    if module.startswith("."):
                        # Relative imports — skip (would need file path
                        # resolution; rare phantom-bug source).
                        pass
                    elif _is_framework_module(module):
                        pass
                    elif not _module_is_first_party(workspace_path, module):
                        # Module doesn't resolve to a file in the workspace
                        # — it's an external package (e.g. `arroyo`, `kombu`).
                        # We can't verify external symbols via workspace grep
                        # without false positives. Skip.
                        pass
                    else:
                        names_chunk = from_match.group(2)
                        for name in _split_import_names(names_chunk):
                            if name in seen_names:
                                continue
                            if checked >= max_symbols_checked:
                                break
                            seen_names.add(name)
                            checked += 1
                            if _python_symbol_defined_anywhere(
                                workspace_path, name,
                                timeout_s=grep_timeout_s,
                            ):
                                continue
                            found.append({
                                "name": name,
                                "referenced_at": (
                                    f"{file_path}:{current_new_line}"
                                ),
                                "evidence": (
                                    f"Deterministic grep for `class {name}`, "
                                    f"`def {name}`, `{name} =` in "
                                    f"`*.py` → 0 matches. Import `from "
                                    f"{module} import {name}` will raise "
                                    f"ImportError at runtime."
                                ),
                            })
                else:
                    bare_match = _PYTHON_BARE_IMPORT_RE.match(raw)
                    if bare_match:
                        module = bare_match.group(1)
                        if (
                            not module.startswith(".")
                            and not _is_framework_module(module)
                        ):
                            # For `import X.Y`, we check if the root module
                            # X has any .py file. Skip for now — bare
                            # imports rarely produce phantom-symbol bugs.
                            pass
            # advance new-line counter for + and context (unchanged)
            if not raw.startswith("-"):
                current_new_line += 1
            if checked >= max_symbols_checked:
                break
        if checked >= max_symbols_checked:
            break
    return found


_FRAMEWORK_MODULE_PREFIXES = (
    "os", "sys", "re", "json", "typing", "logging", "abc", "collections",
    "contextlib", "dataclasses", "enum", "functools", "io", "itertools",
    "math", "pathlib", "random", "subprocess", "time", "unittest",
    "warnings", "asyncio", "concurrent", "datetime", "decimal",
    "django", "flask", "rest_framework", "pydantic", "sqlalchemy",
    "requests", "urllib3", "numpy", "pandas", "pytest", "mypy",
    "starlette", "fastapi", "click", "boto3", "botocore", "sentry_sdk",
)


def _is_framework_module(module: str) -> bool:
    """True if ``module`` is the stdlib or a well-known third-party that
    we never want to verify existence for."""
    if not module:
        return True
    root = module.split(".", 1)[0]
    return root in _FRAMEWORK_MODULE_PREFIXES


def _module_is_first_party(workspace_path: str, module: str) -> bool:
    """True when ``module`` resolves to a file inside the workspace.

    Principled complement to ``_is_framework_module`` — instead of an
    ever-growing blacklist of third-party libraries, we check whether
    the module's expected file-system path exists in the workspace.
    If not, it's an external package (installed via pip) and we should
    not flag its imports as missing — P13 has no way to verify external
    package symbols via workspace grep anyway.

    Checks for both layouts:
      * ``module/path/to/X.py``
      * ``module/path/to/X/__init__.py``

    Returns False on any error / missing workspace (fail-safe: skip).
    """
    import os as _os
    if not workspace_path or not module or module.startswith("."):
        return False
    try:
        candidate = module.replace(".", "/")
        for suffix in (".py", "/__init__.py"):
            if _os.path.exists(_os.path.join(workspace_path, candidate + suffix)):
                return True
        # Also try under common repo layouts: src/<module>, backend/<module>
        for root_prefix in ("src", "backend", "lib"):
            for suffix in (".py", "/__init__.py"):
                if _os.path.exists(
                    _os.path.join(workspace_path, root_prefix, candidate + suffix)
                ):
                    return True
    except Exception:
        return False
    return False


def _split_import_names(names_chunk: str) -> List[str]:
    """Parse the comma-separated tail of a ``from X import ...`` line.

    Handles parentheses, trailing commas, and ``as`` aliases. Filters
    wildcards and keeps only valid Python identifiers."""
    cleaned = names_chunk.strip().strip("()").rstrip(",")
    parts = [p.strip() for p in cleaned.split(",") if p.strip()]
    out: List[str] = []
    for p in parts:
        # Drop the "as alias" portion; we want the imported name.
        primary = p.split(" as ", 1)[0].strip()
        if primary == "*" or not primary.isidentifier():
            continue
        out.append(primary)
    return out


def _python_symbol_defined_anywhere(
    workspace_path: str, name: str, *, timeout_s: float = 8.0,
) -> bool:
    """Grep the workspace for a Python definition of ``name``.

    Matches ``class name``, ``def name``, or ``name = ...`` at line
    start (with optional leading whitespace). Returns True on first
    match; False on zero matches; True on error (fail-safe — never
    report a symbol missing we couldn't verify)."""
    import subprocess

    # Anchor definitions at line start + optional indent only. This
    # avoids matching ``from X import name`` or ``foo(name=...)``.
    # Using extended regex: ^\s*(class|def)\s+name\b  OR
    # ^\s*name\s*=
    pattern = (
        rf"^\s*(class|def)\s+{re.escape(name)}\b|"
        rf"^\s*{re.escape(name)}\s*="
    )
    try:
        r = subprocess.run(
            [
                "grep", "-r", "-E", pattern, workspace_path,
                "--include=*.py", "--max-count=1", "-l",
                "--exclude-dir=.git", "--exclude-dir=.venv",
                "--exclude-dir=node_modules", "--exclude-dir=__pycache__",
            ],
            capture_output=True, text=True, timeout=timeout_s,
        )
        # exit 0 = found ≥1 match; exit 1 = no match; exit 2 = error
        if r.returncode == 0 and r.stdout.strip():
            return True
        # exit 1 = no match → symbol missing
        # any other non-zero = grep error → fail-safe "True" (don't flag)
        return r.returncode != 1
    except Exception:
        return True


# P14 — Mechanical stub-function detector (Python + Go).
# A "stub function" is one whose body unconditionally returns a
# "not implemented" sentinel. In a PR that ostensibly adds new
# functionality, every stub should either be TODO-tagged OR be
# obviously not called — anything else is a bug. We look for two
# shapes:
#   Go  : `return ..., errors.New("not implemented")` / `return errors.New("not implemented")`
#   Py  : `raise NotImplementedError`
# Then we scan the diff for callers of those functions. A call-site
# inside the diff is a strong signal the stub is live code path, not
# a TODO.
_GO_STUB_BODY_RE = re.compile(
    r"""^\s*return\s+                       # return statement
        (?:[^,]+,\s*)?                      # optional first tuple element
        errors\.New\(
        ["'](?:not\ implemented|Not\ Implemented|TODO:?\s*implement)["']
        \)\s*$""",
    re.VERBOSE | re.MULTILINE,
)
_PY_STUB_BODY_RE = re.compile(
    r"^\s*raise\s+NotImplementedError\b", re.MULTILINE,
)
# Java: `throw new UnsupportedOperationException(...)` is the canonical
# "stub" pattern. `NotImplementedException` is Apache Commons. For
# generic runtime exceptions we require the message to mention "not
# implemented" / "not supported" to avoid flagging legitimate errors.
_JAVA_STUB_BODY_RE = re.compile(
    r"""^\s*throw\s+new\s+
        (?:
            UnsupportedOperationException\s*\([^)]*\)
            |
            NotImplementedException\s*\([^)]*\)
            |
            (?:RuntimeException|AssertionError|IllegalStateException)
            \s*\(\s*["'][^"']*
            (?:not\s*implement|Not\s*Implement|not\s*supported|Not\s*Supported)
            [^"']*["']\s*\)
        )
        \s*;\s*$""",
    re.VERBOSE | re.MULTILINE,
)
_GO_FUNC_HEADER_RE = re.compile(
    r"^\+func\s+(?:\(\s*\w+\s+\*?\w+\s*\)\s+)?(\w+)\s*\(",
)
_PY_FUNC_HEADER_RE = re.compile(
    r"^\+\s*def\s+(\w+)\s*\(",
)
# Java method declaration: optional annotations on same line, one or
# more modifiers, optional generic-type parameter, a return type, the
# method name, open paren, closing paren, and opening brace — all on
# the same line. Multi-line signatures are rare in stubs; accept the
# single-line shape as sufficient.
_JAVA_FUNC_HEADER_RE = re.compile(
    r"""^\+\s*
        (?:@\w+(?:\([^)]*\))?\s+)*
        (?:(?:public|private|protected|static|final|synchronized|abstract|default|native)\s+)+
        (?:<[^>]+>\s+)?
        (?:[\w.<>\[\],\s?]+?\s+)?
        (\w+)
        \s*\(""",
    re.VERBOSE,
)
# Same-line marker that a Java code line is (probably) a method
# declaration — used during call-site scanning to exclude declarations
# from being counted as calls when the stub method is also declared in
# the diff (e.g. interface + impl both in scope).
_JAVA_METHOD_DECL_MARKER_RE = re.compile(
    r"^(?:@\w+(?:\([^)]*\))?\s+)*"
    r"(?:(?:public|private|protected|static|final|synchronized|abstract|default|native)\s+)+"
)


def _scan_for_stub_call_sites(
    file_diffs: Dict[str, str],
) -> List[Dict[str, str]]:
    """P14 — Detect stub functions introduced by the PR and match them
    against call sites also in the diff. Returns one dict per detected
    (stub_name, caller_site) pair.

    Operates purely on the diff text; no workspace read. Narrow by
    design: we only flag stubs whose function body in the diff
    contains a literal "not implemented" error return, and we only
    flag call sites that are also added by the diff. This avoids
    flagging legitimate TODO placeholders.
    """
    if not file_diffs:
        return []

    # Step 1: enumerate new stub functions.
    #   { name -> (file, line_in_new_file) }
    stubs: Dict[str, tuple] = {}
    for file_path, diff_text in file_diffs.items():
        is_go = file_path.endswith(".go")
        is_py = file_path.endswith(".py")
        is_java = file_path.endswith(".java")
        if not (is_go or is_py or is_java):
            continue
        # Walk diff line by line tracking hunks + function bodies.
        current_new_line = 0
        current_fn_name: Optional[str] = None
        current_fn_body_lines: List[str] = []
        current_fn_decl_line: int = 0
        for raw in diff_text.splitlines():
            if raw.startswith("@@"):
                m = _DIFF_HUNK_HEADER_RE.match(raw)
                if m:
                    current_new_line = int(m.group(1))
                current_fn_name = None
                current_fn_body_lines = []
                continue
            if raw.startswith("---") or raw.startswith("+++"):
                continue
            is_addition = raw.startswith("+")
            if is_addition:
                if is_go:
                    header_re = _GO_FUNC_HEADER_RE
                elif is_py:
                    header_re = _PY_FUNC_HEADER_RE
                else:  # is_java
                    header_re = _JAVA_FUNC_HEADER_RE
                hm = header_re.match(raw)
                if hm:
                    # New function declaration — reset buffer.
                    current_fn_name = hm.group(1)
                    current_fn_decl_line = current_new_line
                    current_fn_body_lines = [raw]
                elif current_fn_name:
                    current_fn_body_lines.append(raw)
                    # Check for closing `}` (Go or Java) or a stub line
                    # body (Python). For Java the `}` is usually indented
                    # (method-in-class); for Go it's usually column 0.
                    # Accept either case.
                    is_brace_close = (
                        (is_go or is_java)
                        and raw.startswith("+")
                        and raw[1:].strip() == "}"
                    )
                    if is_brace_close:
                        body = "\n".join(
                            ln.lstrip("+ \t")
                            for ln in current_fn_body_lines
                        )
                        body_re = (
                            _GO_STUB_BODY_RE if is_go else _JAVA_STUB_BODY_RE
                        )
                        if body_re.search(body):
                            stubs[current_fn_name] = (
                                file_path, current_fn_decl_line,
                            )
                        current_fn_name = None
                        current_fn_body_lines = []
                    elif is_py:
                        # Strip leading `+` to match against clean code.
                        code_line = raw[1:] if raw.startswith("+") else raw
                        if _PY_STUB_BODY_RE.search(code_line):
                            stubs[current_fn_name] = (
                                file_path, current_fn_decl_line,
                            )
                        # don't reset — Python fn may have more lines
            if not raw.startswith("-"):
                current_new_line += 1

    if not stubs:
        return []

    # Step 2: scan diff for callers of those stub names. Callers can
    # live on + lines (newly added calls) OR unchanged context lines
    # (pre-existing call sites that now hit a NEW stub because the
    # stub's definition was just introduced). We skip - lines (removed)
    # and function-declaration lines (avoid matching `func Name(` or
    # `def Name(` as a call of Name).
    findings: List[Dict[str, str]] = []
    # Call pattern: Name( not preceded by `func ` (Go), `def ` (Python),
    # `type `, `class `, or a period alone (to keep `obj.method(`).
    # Use a look-behind check via pre-filter.
    _DECL_PREFIXES = ("func ", "def ", "type ", "class ")
    for stub_name, (stub_file, stub_line) in stubs.items():
        call_re = re.compile(rf"\b{re.escape(stub_name)}\s*\(")
        for file_path, diff_text in file_diffs.items():
            current_new_line = 0
            seen_sites: set = set()
            for raw in diff_text.splitlines():
                if raw.startswith("@@"):
                    m = _DIFF_HUNK_HEADER_RE.match(raw)
                    if m:
                        current_new_line = int(m.group(1))
                    continue
                if raw.startswith("---") or raw.startswith("+++"):
                    continue
                # Skip removed lines entirely.
                if raw.startswith("-"):
                    continue
                is_code_line = raw.startswith(("+", " "))
                if is_code_line and call_re.search(raw):
                    # Strip leading +/space to examine the code.
                    code = raw[1:] if raw.startswith(("+", " ")) else raw
                    code_stripped = code.lstrip()
                    # Skip function/class declarations that happen to
                    # share the name (`func TablesList(...)` is NOT a
                    # call to TablesList, it's defining a same-name
                    # function in a different package/receiver).
                    is_decl = any(
                        code_stripped.startswith(p) for p in _DECL_PREFIXES
                    )
                    # Java method declarations start with annotations /
                    # modifier keywords (`public Foo(...)`, `@Override
                    # public <T> Foo()`). Treat any line whose stripped
                    # prefix matches that shape as a decl, not a call.
                    if not is_decl and file_path.endswith(".java"):
                        is_decl = bool(
                            _JAVA_METHOD_DECL_MARKER_RE.match(code_stripped)
                        )
                    # Also skip the stub definition line itself.
                    is_self_site = (
                        file_path == stub_file
                        and current_new_line == stub_line
                    )
                    if not is_decl and not is_self_site:
                        key = (file_path, current_new_line)
                        if key not in seen_sites:
                            seen_sites.add(key)
                            findings.append({
                                "stub_name": stub_name,
                                "stub_file": stub_file,
                                "stub_line": str(stub_line),
                                "caller_file": file_path,
                                "caller_line": str(current_new_line),
                            })
                # Advance new-file counter for + and context lines.
                current_new_line += 1
    return findings


def _inject_stub_caller_findings(
    findings: List[Dict[str, Any]],
    file_diffs: Dict[str, str],
) -> tuple[List[Dict[str, Any]], int]:
    """P14 injection — turn (stub, caller) pairs into synthetic findings.

    Each finding points at the CALLER site with a high-confidence
    'calls a stub function that always returns not implemented'
    description. Skips injection if the coordinator already flagged
    the caller site at approximately the same line (±3).

    Returns (findings_with_injections, injected_count).
    """
    if not file_diffs:
        return (findings, 0)

    pairs = _scan_for_stub_call_sites(file_diffs)
    if not pairs:
        return (findings, 0)

    result = list(findings)
    injected = 0
    for p in pairs:
        caller_file = p["caller_file"]
        try:
            caller_line = int(p["caller_line"])
        except (ValueError, TypeError):
            continue
        stub_name = p["stub_name"]
        # Skip if an existing finding covers this (file, ±3 lines).
        covered = False
        for f in result:
            if f.get("file") != caller_file:
                continue
            fl = int(f.get("start_line") or 0)
            if abs(fl - caller_line) <= 3:
                # Also check the finding mentions the stub or concept.
                title = str(f.get("title", "") or "")
                if (
                    stub_name in title
                    or "stub" in title.lower()
                    or "not implemented" in title.lower()
                ):
                    covered = True
                    break
        if covered:
            continue
        synthetic = {
            "title": (
                f"Call to `{stub_name}()` hits a stub that always "
                f"returns 'not implemented' — runtime failure"
            ),
            "severity": "high",
            "confidence": 0.95,
            "file": caller_file,
            "start_line": caller_line,
            "end_line": caller_line,
            "evidence": [
                f"Stub definition at `{p['stub_file']}:{p['stub_line']}` "
                f"returns an error literal 'not implemented' / "
                f"NotImplementedError.",
                f"Call site at `{caller_file}:{caller_line}` invokes "
                f"the stub and does not guard against the failure.",
            ],
            "risk": (
                f"Every code path that reaches `{caller_file}:"
                f"{caller_line}` will surface the 'not implemented' "
                f"error to the user. The feature being implemented in "
                f"this PR is unshippable until the stub is filled in."
            ),
            "suggested_fix": (
                f"Either implement `{stub_name}` at "
                f"`{p['stub_file']}:{p['stub_line']}` with real logic, "
                f"or gate the caller behind a feature flag / explicit "
                f"'unsupported' error response until the implementation "
                f"lands."
            ),
            "category": "correctness",
            "_injected_from": "p14_stub_caller",
        }
        result.append(synthetic)
        injected += 1
    return (result, injected)


_EXISTS_NEGATION_MARKERS = (
    "does not exist",
    "doesn't exist",
    "not defined",
    "undefined",
    "is missing",
    "never defined",
    "no such symbol",
    "importerror",
    "nameerror",
    "not found in",
    "could not be found",
)


def _finding_claims_symbol_missing(finding: Dict[str, Any], symbol: str) -> bool:
    """Heuristic: does this finding claim `symbol` is missing/undefined?

    Used by the Phase 2 reflection pass to catch findings whose premise
    contradicts an `exists=True` fact. We match only when the finding
    BOTH mentions the symbol AND uses existence-negation phrasing —
    mentioning the symbol alone is not enough (many real bugs involve
    existing symbols)."""
    if not symbol:
        return False
    haystack_parts: List[str] = [
        str(finding.get("title", "") or ""),
        str(finding.get("risk", "") or ""),
        str(finding.get("suggested_fix", "") or ""),
    ]
    evidence = finding.get("evidence")
    if isinstance(evidence, list):
        haystack_parts.extend(str(e) for e in evidence)
    elif isinstance(evidence, str):
        haystack_parts.append(evidence)
    haystack = " ".join(haystack_parts)
    if symbol not in haystack:
        return False
    lowered = haystack.lower()
    return any(marker in lowered for marker in _EXISTS_NEGATION_MARKERS)


def _reflect_against_phase2_facts(
    findings: List[Dict[str, Any]],
) -> tuple[List[Dict[str, Any]], int]:
    """External-signal reflection (P8).

    Drops findings whose premise is contradicted by Phase 2 existence
    facts. The mechanical rule — deliberately narrow to avoid over-
    filtering — is:

        If the finding claims "symbol X doesn't exist / is undefined /
        will raise ImportError" AND Phase 2 recorded ``exists=True`` for
        X, drop the finding. Its premise is demonstrably wrong.

    Injected Phase 2 findings (``_injected_from`` set) are never dropped
    by this pass — they came FROM the facts, so they cannot contradict.

    Returns (kept_findings, dropped_count). Safe when no FactStore is
    active (returns input unchanged).
    """
    from app.scratchpad import current_factstore

    store = current_factstore()
    if store is None:
        return (findings, 0)

    try:
        present = list(store.iter_existence(exists=True))
    except Exception as exc:
        logger.warning(
            "[PR Brain v2] reflection pass skipped — iter_existence failed: %s",
            exc,
        )
        return (findings, 0)

    if not present:
        return (findings, 0)

    present_symbols = {p.symbol_name for p in present if p.symbol_name}
    if not present_symbols:
        return (findings, 0)

    kept: List[Dict[str, Any]] = []
    dropped = 0
    for f in findings:
        if f.get("_injected_from"):
            kept.append(f)
            continue
        contradicted = False
        for symbol in present_symbols:
            if _finding_claims_symbol_missing(f, symbol):
                logger.info(
                    "[PR Brain v2] Reflection drop: finding %r claims "
                    "`%s` is missing but Phase 2 confirmed exists=True",
                    f.get("title", "")[:80], symbol,
                )
                contradicted = True
                break
        if contradicted:
            dropped += 1
        else:
            kept.append(f)
    return (kept, dropped)


def _filter_findings_to_diff_scope(
    findings: List[Dict[str, Any]],
    file_diffs: Dict[str, str],
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], int]:
    """Per-finding diff-scope verification (P11 cheap).

    Inspired by Claude Code `/ultrareview`'s "every reported finding is
    independently reproduced and verified". This is the mechanical half
    of that pattern — an LLM-free check that a finding's file is actually
    touched by the diff. Findings that point at files the PR does not
    modify are almost always coordinator hallucinations (e.g. it confused
    a cross-file reference with a diff change).

    Kept injected findings untouched — Phase 2 may flag a diff file's
    reference to a symbol defined in an un-touched file, and that is
    legitimate scope.

    Returns (kept, demoted, demoted_count). Demoted findings are handed
    back so the caller can append them to the secondary-notes block.
    """
    if not file_diffs or not findings:
        return (list(findings), [], 0)

    touched_files = set(file_diffs.keys())
    # Allow trailing-slash / normalisation mismatches by also matching
    # basename when the coordinator reported a short path.
    touched_basenames = {p.rsplit("/", 1)[-1] for p in touched_files}

    kept: List[Dict[str, Any]] = []
    demoted: List[Dict[str, Any]] = []
    demoted_count = 0

    for f in findings:
        if f.get("_injected_from"):
            kept.append(f)
            continue
        file_claim = str(f.get("file", "") or "").strip()
        if not file_claim:
            kept.append(f)
            continue
        base = file_claim.rsplit("/", 1)[-1]
        in_diff = file_claim in touched_files or base in touched_basenames
        if in_diff:
            kept.append(f)
            continue
        logger.info(
            "[PR Brain v2] Diff-scope drop: finding %r targets `%s` "
            "which is not in the PR diff (touched: %d files)",
            f.get("title", "")[:80], file_claim, len(touched_files),
        )
        f = {**f, "_demoted_reason": "file_not_in_diff"}
        demoted.append(f)
        demoted_count += 1

    return (kept, demoted, demoted_count)


def _extract_single_verdict(raw: str) -> str:
    """Parse the single-finding verifier's JSON. Returns one of
    confirmed / refuted / unclear. Defaults to unclear on parse failure."""
    import json as _json
    import re as _re

    fenced = _re.findall(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", raw)
    candidates = list(reversed(fenced)) if fenced else [raw[max(0, raw.rfind("{")):]]
    for candidate in candidates:
        try:
            parsed = _json.loads(candidate)
            if isinstance(parsed, dict) and "verdict" in parsed:
                v = str(parsed["verdict"]).lower()
                if v in ("confirmed", "refuted", "unclear"):
                    return v
        except (ValueError, _json.JSONDecodeError):
            continue
    return "unclear"


def _extract_batch_verdicts(raw: str, expected_count: int) -> List[str]:
    """Parse the batch verifier's JSON. Returns verdict list aligned to
    input order. Missing / malformed entries default to 'unclear'."""
    import json as _json
    import re as _re

    fenced = _re.findall(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", raw)
    candidates = list(reversed(fenced))
    for candidate in candidates:
        try:
            parsed = _json.loads(candidate)
            if not isinstance(parsed, dict):
                continue
            verdicts_list = parsed.get("verdicts") or []
            if not isinstance(verdicts_list, list):
                continue
            # Build index→verdict map first so out-of-order lists are handled.
            verdict_map: Dict[int, str] = {}
            for item in verdicts_list:
                if not isinstance(item, dict):
                    continue
                idx = item.get("finding_index")
                v = str(item.get("verdict", "")).lower()
                if isinstance(idx, int) and v in ("confirmed", "refuted", "unclear"):
                    verdict_map[idx] = v
            if verdict_map:
                return [verdict_map.get(i, "unclear") for i in range(expected_count)]
        except (ValueError, _json.JSONDecodeError):
            continue
    return ["unclear"] * expected_count


def _parse_existence_json(raw: str) -> Optional[Dict[str, Any]]:
    """Best-effort extraction of the existence-worker's JSON output.

    Accepts:
      * Fenced ```json {...} ``` blocks (prefer the LAST — models often
        restate near the end)
      * Bare JSON object with "symbols" key anywhere in the text

    Returns the dict on success, ``None`` on failure.
    """
    import json as _json
    import re as _re

    if not raw:
        return None

    fenced = _re.findall(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", raw)
    candidates: list = list(reversed(fenced))
    if not candidates:
        # Fallback: find a top-level {..} with "symbols" key
        for start in range(len(raw) - 1, -1, -1):
            if raw[start] != "{":
                continue
            depth = 0
            for end in range(start, len(raw)):
                if raw[end] == "{":
                    depth += 1
                elif raw[end] == "}":
                    depth -= 1
                    if depth == 0:
                        snippet = raw[start: end + 1]
                        if '"symbols"' in snippet:
                            candidates.append(snippet)
                        break
            if candidates:
                break
    for candidate in candidates:
        try:
            parsed = _json.loads(candidate)
            if isinstance(parsed, dict) and "symbols" in parsed:
                return parsed
        except (ValueError, _json.JSONDecodeError):
            continue
    return None


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
