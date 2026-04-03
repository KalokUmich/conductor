"""Specialized review agent definitions.

Each agent has a focused prompt, tool set, and budget tailored to its
review dimension. The orchestrator (CodeReviewService) dispatches them
in parallel and merges their findings.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

from app.agent_loop.budget import BudgetConfig
from app.agent_loop.service import AgentLoopService, AgentResult
from app.ai_provider.base import AIProvider

from .models import (
    AgentReviewResult,
    FindingCategory,
    PRContext,
    ReviewFinding,
    RiskLevel,
    RiskProfile,
)
from .shared import (
    FOCUS_DESCRIPTIONS as _FOCUS_DESCRIPTIONS,
)
from .shared import (
    build_diffs_section as _build_diffs_section,
)
from .shared import (
    evidence_gate as _evidence_gate_shared,
)
from .shared import (
    parse_findings as _parse_findings_shared,
)
from .shared import (
    repair_output as _repair_output_shared,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Agent specifications
# ---------------------------------------------------------------------------

# Core tools available to all review agents
_REVIEW_CORE_TOOLS = [
    "grep",
    "read_file",
    "find_symbol",
    "file_outline",
    "compressed_view",
    "expand_symbol",
]


@dataclass
class AgentSpec:
    """Specification for a specialized review agent."""

    name: str
    category: FindingCategory
    tools: List[str]  # tool names (subset of 21)
    budget_tokens: int  # max input tokens
    max_iterations: int
    risk_dimensions: List[str]  # which risk dimensions trigger this agent
    strategy_hint: str = ""  # per-agent investigation strategy

    def should_run(self, risk_profile: RiskProfile, always_run: bool = False) -> bool:
        """Decide if this agent should be dispatched based on risk."""
        if always_run:
            return True
        for dim in self.risk_dimensions:
            level = getattr(risk_profile, dim, RiskLevel.LOW)
            if level in (RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL):
                return True
        return False


# ---------------------------------------------------------------------------
# Budget anchors — sub-agents get SUB_FRACTION of the main agent's defaults.
# Per-agent weights express relative complexity (correctness = heaviest).
# The PR-size multiplier in service.py is applied on top of these values.
#
#   budget_tokens = _MAIN_TOKENS × _SUB_FRACTION × weight
#   max_iterations = _MAIN_ITERS  × _SUB_FRACTION × weight  (rounded)
#
# Agent weights:
#   correctness  1.00  — cross-file data-flow tracing, heaviest tool set
#   concurrency  0.85  — shared-state analysis, still deep but narrower scope
#   security     0.75  — taint tracing + AST search, focused scope
#   reliability  0.70  — call-chain + git history, moderate depth
#   test_coverage 0.55 — file listing + test lookup, lightest
# ---------------------------------------------------------------------------
_MAIN_TOKENS: int = 880_000  # mirrors AgentLoopService / BudgetConfig default
_MAIN_ITERS: int = 40  # mirrors AgentLoopService default
_SUB_FRACTION: float = 0.7  # sub-agents get 70% of the main budget


def _sub_budget(weight: float) -> int:
    return int(_MAIN_TOKENS * _SUB_FRACTION * weight)


def _sub_iters(weight: float) -> int:
    return max(int(_MAIN_ITERS * _SUB_FRACTION * weight), 8)


# Agent registry — each agent focuses on one dimension
AGENT_SPECS: List[AgentSpec] = [
    AgentSpec(
        name="correctness",
        category=FindingCategory.CORRECTNESS,
        tools=_REVIEW_CORE_TOOLS
        + [
            "git_diff",
            "git_show",
            "git_log",
            "find_references",
            "get_callers",
            "get_callees",
            "trace_variable",
            "get_dependencies",
        ],
        budget_tokens=_sub_budget(1.00),  # 616,000
        max_iterations=_sub_iters(1.00),  # 28
        risk_dimensions=["correctness"],
        strategy_hint=(
            "Mixed strategy: scan all diffs for suspicious patterns first, "
            "then deep-dive the top 2-3 suspects with trace_variable and get_callees. "
            "Use git_show to compare code BEFORE vs AFTER the change. "
            "Budget 3-4 tool calls for scanning, 6-8 for deep investigation."
        ),
    ),
    AgentSpec(
        name="concurrency",
        category=FindingCategory.CONCURRENCY,
        tools=_REVIEW_CORE_TOOLS
        + [
            "git_diff",
            "git_show",
            "find_references",
            "get_callers",
            "get_callees",
            "trace_variable",
            "ast_search",
        ],
        budget_tokens=_sub_budget(0.85),  # 523,600
        max_iterations=_sub_iters(0.85),  # 23
        risk_dimensions=["concurrency"],
        strategy_hint=(
            "Depth-first: identify shared-state operations, then trace each "
            "to check atomicity. Use ast_search for check-then-act patterns. "
            "Spend most tool calls proving or disproving one race at a time."
        ),
    ),
    AgentSpec(
        name="security",
        category=FindingCategory.SECURITY,
        tools=_REVIEW_CORE_TOOLS
        + [
            "git_diff",
            "git_show",
            "git_log",
            "trace_variable",
            "find_references",
            "git_blame",
            "ast_search",
        ],
        budget_tokens=_sub_budget(0.75),  # 462,000
        max_iterations=_sub_iters(0.75),  # 21
        risk_dimensions=["security"],
        strategy_hint=(
            "Depth-first: trace data from external input (HTTP, queue, file) "
            "through to storage/output. Use trace_variable for taint analysis. "
            "Use git_log search= to find related security fixes or CVEs. "
            "For each flow, verify sanitization/validation at every boundary."
        ),
    ),
    AgentSpec(
        name="reliability",
        category=FindingCategory.RELIABILITY,
        tools=_REVIEW_CORE_TOOLS
        + [
            "git_diff",
            "get_callers",
            "find_references",
            "git_log",
            "git_show",
        ],
        budget_tokens=_sub_budget(0.70),  # 431,200
        max_iterations=_sub_iters(0.70),  # 19
        risk_dimensions=["reliability", "operational"],
        strategy_hint=(
            "Breadth-first: check every exception handler, resource acquisition, "
            "and error path in the changed files. Use get_callers to verify "
            "callers handle errors. Brief checks across many paths > deep dive on one."
        ),
    ),
    AgentSpec(
        name="test_coverage",
        category=FindingCategory.TEST_COVERAGE,
        tools=_REVIEW_CORE_TOOLS
        + [
            "git_diff",
            "find_tests",
            "test_outline",
            "find_references",
            "list_files",
            "run_test",
        ],
        budget_tokens=_sub_budget(0.55),  # 338,800
        max_iterations=_sub_iters(0.55),  # 15
        risk_dimensions=[],  # always runs (via always_run flag)
        strategy_hint=(
            "Breadth-first: for each changed file, use find_tests to locate "
            "existing tests. Use test_outline on found test files to assess "
            "coverage quality. Use run_test to execute key tests and verify "
            "they still pass. Focus on untested critical paths, not line counts."
        ),
    ),
]


# ---------------------------------------------------------------------------
# Agent prompt templates
# ---------------------------------------------------------------------------

_AGENT_PROMPT_TEMPLATE = """\
You are a **{agent_name} reviewer** performing a focused code review.

## HARD CONSTRAINT — The Provability Test

Before assigning any severity, answer: "Can I prove this from the CODE ALONE,
or does my conclusion depend on an unverified business/design assumption?"

- **Code-provable defect**: The code's own structure guarantees incorrect behavior
  regardless of design intent. Example: a check-then-act race where two concurrent
  requests both pass a non-atomic validation — broken no matter what the designer intended.
- **Assumption-dependent concern**: Severity depends on what the designer meant.
  Example: "token not consumed on failure" — if design intends one-time-use, it's a bug;
  if design intends retry-until-success, it's correct. You cannot know which.

**Rule: assumption-dependent concerns are capped at warning.** Qualify them:
"If the intended design is X, then Y is a defect." Never state them as definitive bugs.
Prefer code-structural defects over business-semantic assumptions.

## Severity levels

- **critical**: Code-provable defect that WILL cause incorrect behavior, data loss, or
  security breach. Construct a concrete trigger scenario from code facts only. "Missing
  tests" is NEVER critical.
- **warning**: (a) Code-provable risk where trigger is not fully proven reachable, OR
  (b) assumption-dependent concern where likely intent suggests a defect. Missing tests
  for critical paths belong here.
- **nit**: Style, naming, minor improvement, or speculative concern.
- **praise**: Notably good code — clear design, thorough error handling, etc.

## Your review focus: {focus_description}

## Investigation strategy
{strategy_hint}

<pr_context>
diff_spec: {diff_spec}
files: {file_count} ({total_lines} lines changed)
risk: {risk_summary}
</pr_context>

<file_list>
{file_list}
</file_list>

<diffs>
{diffs_section}
</diffs>

{impact_context_section}
## Investigation instructions
1. Analyze the diffs above for issues in your focus area.
2. Use **read_file** with line ranges for broader context around changes.
3. Use **git_show** with a commit ref and file path to see the code BEFORE the change — compare what was removed/replaced to understand intent.
4. Use **git_log** with search= to find related commits (e.g. search="CVE", search="fix timeout").
5. Use additional tools (find_references, get_callers, trace_variable, etc.) to trace impact.
6. The file list and diffs are already provided — skip git_diff_files.
7. When you have enough evidence, stop investigating and produce your findings JSON.

## Quality rules
- Report at most **5 findings**. Prioritize by real-world impact.
- Each finding must cite specific file:line from the diff or surrounding code.
- One finding per root cause — merge related angles into a single finding.
- When uncertain about severity, downgrade by one level.
- Set confidence honestly: 0.9+ only if you traced the full path and are certain;
  0.7-0.8 for well-evidenced but not fully traced; below 0.6 = omit.
- Assume config/infra works as deployed. Review the code as written.

## Output format — MANDATORY

Your ONLY deliverable is a JSON array. Output it as your final message with no
commentary before or after.

### Example 1 — code-provable Critical
```json
[
  {{
    "title": "Non-atomic check-then-act race in token validation",
    "severity": "critical",
    "confidence": 0.92,
    "file": "src/auth/TokenService.java",
    "start_line": 266,
    "end_line": 330,
    "evidence": [
      "checkToken() at line 266 performs GET, consumeToken() at line 330 performs DELETE",
      "Two concurrent Lambda retries can both pass checkToken() before either consumes"
    ],
    "risk": "Duplicate processing: two callbacks execute the same business logic",
    "suggested_fix": "Replace separate check+consume with a single atomic GETDEL operation"
  }}
]
```

### Example 2 — assumption-dependent Warning
```json
[
  {{
    "title": "Webhook token not consumed on technical failure paths",
    "severity": "warning",
    "confidence": 0.75,
    "file": "src/callback/CallbackService.java",
    "start_line": 309,
    "end_line": 319,
    "evidence": [
      "catch block at line 309-319 logs error but does not call consumeToken()",
      "Token remains valid in Redis for the full 12h TTL"
    ],
    "risk": "If the intended security model is strict one-time-use, technical failures leave the token replayable",
    "suggested_fix": "If one-time-use is intended: move consumeToken() into a finally block"
  }}
]
```

If you find no issues, output exactly: `[]`

RULES:
- severity MUST be one of: "critical", "warning", "nit", "praise"
- confidence MUST be a number between 0.0 and 1.0
- evidence MUST be an array of strings
- If your token budget is running low, output your findings JSON IMMEDIATELY"""


def _build_agent_query(
    spec: AgentSpec,
    pr_context: PRContext,
    risk_profile: RiskProfile,
    file_diffs: Optional[Dict[str, str]] = None,
    impact_context: str = "",
) -> str:
    """Build the agent query string from the spec and PR context."""
    # Build file list (top files by change size)
    files = pr_context.business_logic_files()
    if spec.name == "test_coverage":
        files = pr_context.files  # test agent sees all files
    elif spec.name == "security":
        # Security agent also sees config files
        files = pr_context.business_logic_files() + pr_context.config_files()

    file_list_lines = []
    for f in files[:20]:  # cap at 20 files
        file_list_lines.append(f"- `{f.path}` ({f.status}, +{f.additions}/-{f.deletions})")
    file_list = "\n".join(file_list_lines) if file_list_lines else "- (no files in scope)"

    risk_summary = (
        f"correctness={risk_profile.correctness.value}, "
        f"concurrency={risk_profile.concurrency.value}, "
        f"security={risk_profile.security.value}, "
        f"reliability={risk_profile.reliability.value}, "
        f"operational={risk_profile.operational.value}"
    )

    diffs_section = _build_diffs_section(files, file_diffs or {})

    # Build impact context section (only if content is available)
    if impact_context:
        impact_section = f"<impact_context>\n{impact_context}\n</impact_context>\n\n"
    else:
        impact_section = ""

    return _AGENT_PROMPT_TEMPLATE.format(
        agent_name=spec.name.replace("_", " ").title(),
        diff_spec=pr_context.diff_spec,
        file_count=pr_context.file_count,
        total_lines=pr_context.total_changed_lines,
        risk_summary=risk_summary,
        file_list=file_list,
        focus_description=_FOCUS_DESCRIPTIONS.get(spec.name, "General code quality"),
        diffs_section=diffs_section,
        strategy_hint=spec.strategy_hint or "Investigate the highest-impact issues first.",
        impact_context_section=impact_section,
    )


# ---------------------------------------------------------------------------
# Finding extraction — thin wrappers around shared.py
# ---------------------------------------------------------------------------


def _parse_findings(
    answer: str,
    spec: AgentSpec,
    *,
    warn_on_empty: bool = True,
) -> List[ReviewFinding]:
    """Extract structured findings from an agent's answer text."""
    return _parse_findings_shared(
        answer,
        spec.name,
        spec.category,
        warn_on_empty=warn_on_empty,
    )


async def _repair_output(
    answer: str,
    spec: AgentSpec,
    provider: AIProvider,
) -> List[ReviewFinding]:
    """Attempt to recover findings by asking the model to reformat the answer."""
    return await _repair_output_shared(answer, spec.name, spec.category, provider)


def _evidence_gate(findings: List[ReviewFinding], agent_result: AgentResult) -> List[ReviewFinding]:
    """Validate evidence quality for Critical findings."""
    tool_calls = agent_result.tool_calls_made if agent_result else 0
    return _evidence_gate_shared(findings, tool_calls)


# ---------------------------------------------------------------------------
# Agent execution
# ---------------------------------------------------------------------------


async def run_review_agent(
    spec: AgentSpec,
    pr_context: PRContext,
    risk_profile: RiskProfile,
    provider: AIProvider,
    workspace_path: str,
    trace_writer=None,
    file_diffs: Optional[Dict[str, str]] = None,
    llm_semaphore: Optional[asyncio.Semaphore] = None,
    impact_context: str = "",
) -> AgentReviewResult:
    """Run a single specialized review agent.

    Creates an AgentLoopService with the agent's budget and tool set,
    runs the review query, and parses findings from the answer.

    Args:
        provider: The AI provider for the iterative review loop.  Typically
            the lightweight classifier model (e.g. Haiku 4.5).
        llm_semaphore: Optional shared semaphore to limit concurrent LLM
            API calls across parallel agents (prevents Bedrock throttling).
    """
    query = _build_agent_query(spec, pr_context, risk_profile, file_diffs, impact_context)

    budget = BudgetConfig(
        max_input_tokens=spec.budget_tokens,
        max_iterations=spec.max_iterations,
    )

    agent = AgentLoopService(
        provider=provider,
        max_iterations=spec.max_iterations,
        budget_config=budget,
        trace_writer=trace_writer,
        _is_sub_agent=True,
        llm_semaphore=llm_semaphore,
    )

    try:
        result: AgentResult = await agent.run(
            query=query,
            workspace_path=workspace_path,
        )

        findings = _parse_findings(result.answer, spec)

        # If no findings were parsed but the agent produced context chunks,
        # try to extract findings from individual context chunks too.
        # This helps when FORCE_CONCLUDE cuts off the agent before it
        # produces a final JSON block — partial findings may be scattered
        # across accumulated text.
        if not findings and result.context_chunks:
            for chunk in result.context_chunks:
                chunk_findings = _parse_findings(
                    chunk.content,
                    spec,
                    warn_on_empty=False,
                )
                findings.extend(chunk_findings)
            if findings:
                logger.info(
                    "Recovered %d findings from context chunks for %s agent",
                    len(findings),
                    spec.name,
                )

        # --- Improvement 1: Output Repair Loop ---
        # If no findings but the agent produced text, ask the model to
        # reformat the answer as JSON (one cheap extra call).
        if not findings and result.answer and len(result.answer) > 50:
            logger.info(
                "Agent '%s' produced %d chars but no parseable findings — attempting repair loop",
                spec.name,
                len(result.answer),
            )
            findings = await _repair_output(result.answer, spec, provider)

        if not findings and result.answer:
            logger.warning(
                "Agent '%s' produced %d chars of text but no parseable findings "
                "(even after repair). First 200 chars: %s",
                spec.name,
                len(result.answer),
                result.answer[:200],
            )

        # --- Improvement 3: Evidence Gate ---
        # Downgrade Critical findings that lack sufficient evidence.
        if findings:
            findings = _evidence_gate(findings, result)

        tokens = 0
        if result.budget_summary:
            tokens = result.budget_summary.get("total_input_tokens", 0) + result.budget_summary.get(
                "total_output_tokens", 0
            )

        return AgentReviewResult(
            agent_name=spec.name,
            findings=findings,
            summary=result.answer,
            tokens_used=tokens,
            iterations=result.iterations,
            duration_ms=result.duration_ms,
            error=result.error,
        )
    except Exception as e:
        logger.error("Review agent '%s' failed: %s", spec.name, e)
        return AgentReviewResult(
            agent_name=spec.name,
            error=str(e),
        )
