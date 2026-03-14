"""Specialized review agent definitions.

Each agent has a focused prompt, tool set, and budget tailored to its
review dimension. The orchestrator (CodeReviewService) dispatches them
in parallel and merges their findings.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

from app.agent_loop.budget import BudgetConfig
from app.agent_loop.service import AgentLoopService, AgentResult
from app.ai_provider.base import AIProvider

from .models import (
    AgentReviewResult,
    ChangedFile,
    FindingCategory,
    PRContext,
    ReviewFinding,
    RiskLevel,
    RiskProfile,
    Severity,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Agent specifications
# ---------------------------------------------------------------------------

# Core tools available to all review agents
_REVIEW_CORE_TOOLS = [
    "grep", "read_file", "find_symbol", "file_outline",
    "compressed_view", "expand_symbol",
]


@dataclass
class AgentSpec:
    """Specification for a specialized review agent."""
    name: str
    category: FindingCategory
    tools: List[str]                  # tool names (subset of 21)
    budget_tokens: int                # max input tokens
    max_iterations: int
    risk_dimensions: List[str]        # which risk dimensions trigger this agent

    def should_run(self, risk_profile: RiskProfile, always_run: bool = False) -> bool:
        """Decide if this agent should be dispatched based on risk."""
        if always_run:
            return True
        for dim in self.risk_dimensions:
            level = getattr(risk_profile, dim, RiskLevel.LOW)
            if level in (RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL):
                return True
        return False


# Agent registry — each agent focuses on one dimension
AGENT_SPECS: List[AgentSpec] = [
    AgentSpec(
        name="correctness",
        category=FindingCategory.CORRECTNESS,
        tools=_REVIEW_CORE_TOOLS + [
            "git_diff", "find_references",
            "get_callers", "get_callees", "trace_variable",
            "get_dependencies",
        ],
        budget_tokens=400_000,
        max_iterations=20,
        risk_dimensions=["correctness"],
    ),
    AgentSpec(
        name="concurrency",
        category=FindingCategory.CONCURRENCY,
        tools=_REVIEW_CORE_TOOLS + [
            "git_diff", "find_references",
            "get_callers", "get_callees", "trace_variable",
            "ast_search",
        ],
        budget_tokens=350_000,
        max_iterations=18,
        risk_dimensions=["concurrency"],
    ),
    AgentSpec(
        name="security",
        category=FindingCategory.SECURITY,
        tools=_REVIEW_CORE_TOOLS + [
            "git_diff", "trace_variable",
            "find_references", "git_blame", "ast_search",
        ],
        budget_tokens=300_000,
        max_iterations=15,
        risk_dimensions=["security"],
    ),
    AgentSpec(
        name="reliability",
        category=FindingCategory.RELIABILITY,
        tools=_REVIEW_CORE_TOOLS + [
            "git_diff", "get_callers",
            "find_references", "git_log", "git_show",
        ],
        budget_tokens=300_000,
        max_iterations=15,
        risk_dimensions=["reliability", "operational"],
    ),
    AgentSpec(
        name="test_coverage",
        category=FindingCategory.TEST_COVERAGE,
        tools=_REVIEW_CORE_TOOLS + [
            "git_diff", "find_tests",
            "test_outline", "find_references", "list_files",
        ],
        budget_tokens=250_000,
        max_iterations=12,
        risk_dimensions=[],   # always runs (via always_run flag)
    ),
]


# ---------------------------------------------------------------------------
# Agent prompt templates
# ---------------------------------------------------------------------------

_AGENT_PROMPT_TEMPLATE = """\
You are a **{agent_name} reviewer** performing a focused code review on a PR.

## PR Info
- Diff spec: `{diff_spec}`
- Total files: {file_count} ({total_lines} lines changed)
- Risk profile: {risk_summary}

## Files in scope
{file_list}

## Your review focus: {focus_description}

## Diffs (pre-fetched)
The diffs for files in your scope are provided below. Analyze them directly.
{diffs_section}

## Instructions
1. Analyze the diffs above for issues in your focus area
2. Use **read_file** with line ranges around changes for broader context
3. Use additional tools (find_references, get_callers, trace_variable, etc.) to trace impact
4. Do NOT call git_diff_files — the file list and diffs are already provided above

## Severity criteria — follow strictly
- **critical**: A provable bug that WILL cause incorrect behavior, data loss, or security
  breach in production. You must be able to construct a concrete scenario (specific input
  or sequence of events) that triggers the bug. "Missing tests" is NEVER critical.
  "Theoretical attack requiring compromised config" is NEVER critical.
- **warning**: A real code smell or risky pattern that COULD cause issues under specific
  conditions. You have evidence the condition is reachable but cannot fully prove it
  triggers in practice. Missing tests for critical paths belong here.
- **nit**: Style, naming, minor improvement, or a speculative concern without strong evidence.
- **praise**: Notably good code — clear design, thorough error handling, etc.

## Quality rules
- Report at most **5 findings**. Prioritize by real-world impact.
- Each finding must cite specific line numbers from the diff or surrounding code.
- Do NOT report the same root cause multiple times from different angles.
- Do NOT inflate severity. If you are unsure, downgrade by one level.
- Set confidence honestly: 0.9+ only if you traced the full code path and are certain.
  0.7-0.8 for well-evidenced but not fully traced. Below 0.6 = do not report.
- "Missing test coverage" alone is a warning at most, never critical.
- Do NOT assume config/infra is compromised. Review the code as written.

## Output format
After reviewing, output your findings as a JSON array:
```json
[
  {{
    "title": "Brief issue title",
    "severity": "critical|warning|nit|praise",
    "confidence": 0.0-1.0,
    "file": "path/to/file.py",
    "start_line": 42,
    "end_line": 55,
    "evidence": ["evidence point 1", "evidence point 2"],
    "risk": "What could go wrong and why",
    "suggested_fix": "How to fix it"
  }}
]
```

If you find no issues in your focus area, return an empty array `[]`.
Be precise. Only report issues you have evidence for. Avoid speculation."""


_FOCUS_DESCRIPTIONS = {
    "correctness": (
        "Logic errors, null/undefined access, off-by-one, race conditions, "
        "wrong conditionals, missing edge cases, breaking API contracts, "
        "state machine violations, incorrect error handling."
    ),
    "concurrency": (
        "Check-then-act patterns, duplicate processing, token/lock lifecycle, "
        "callback replay, queue redelivery safety, retry idempotency, "
        "thread safety, deadlock potential."
    ),
    "security": (
        "Injection vulnerabilities (SQL, XSS, command), auth bypass, "
        "secrets in code, insecure defaults, missing input validation, "
        "sensitive data in logs, replay attacks, CSRF/CORS issues."
    ),
    "reliability": (
        "Swallowed exceptions, missing error handling, timeout issues, "
        "resource leaks, missing observability (logging/metrics), "
        "hardcoded config, shutdown behavior, DLQ/retry gaps."
    ),
    "test_coverage": (
        "New logic without test coverage, untested failure paths, "
        "tests that don't assert meaningful behavior, missing edge case tests, "
        "untested concurrent/async paths."
    ),
}


# Max chars per individual file diff before truncation.
# 15KB covers ~400 lines of unified diff with context — enough for
# most files to avoid agents needing to re-fetch via read_file.
_MAX_FILE_DIFF_CHARS = 15_000
# Max total chars of diffs injected into an agent prompt (~30-40K tokens).
# Agents have 250K-600K token budgets, so this is a manageable fraction.
_MAX_TOTAL_DIFF_CHARS = 120_000


def _build_diffs_section(
    files: List[ChangedFile],
    file_diffs: Dict[str, str],
) -> str:
    """Build the pre-fetched diffs section for the agent prompt.

    Includes diffs for the agent's scoped files, truncating large diffs
    and capping total size to avoid blowing up the context window.
    """
    if not file_diffs:
        return "(diffs not available — use git_diff to fetch as needed)"

    sections = []
    total_chars = 0

    for f in files[:20]:
        diff_text = file_diffs.get(f.path, "")
        if not diff_text:
            continue

        # Truncate individual large diffs
        if len(diff_text) > _MAX_FILE_DIFF_CHARS:
            diff_text = diff_text[:_MAX_FILE_DIFF_CHARS] + \
                f"\n... (truncated, {len(file_diffs[f.path]):,} chars total — use read_file for full content)"

        # Check total budget
        if total_chars + len(diff_text) > _MAX_TOTAL_DIFF_CHARS:
            remaining = len(files) - len(sections)
            sections.append(
                f"\n... ({remaining} more file(s) omitted — use git_diff to view)"
            )
            break

        sections.append(f"### `{f.path}`\n```diff\n{diff_text}\n```")
        total_chars += len(diff_text)

    if not sections:
        return "(no diffs available for files in scope)"

    return "\n\n".join(sections)


def _build_agent_query(
    spec: AgentSpec,
    pr_context: PRContext,
    risk_profile: RiskProfile,
    file_diffs: Optional[Dict[str, str]] = None,
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
        file_list_lines.append(
            f"- `{f.path}` ({f.status}, +{f.additions}/-{f.deletions})"
        )
    file_list = "\n".join(file_list_lines) if file_list_lines else "- (no files in scope)"

    risk_summary = (
        f"correctness={risk_profile.correctness.value}, "
        f"concurrency={risk_profile.concurrency.value}, "
        f"security={risk_profile.security.value}, "
        f"reliability={risk_profile.reliability.value}, "
        f"operational={risk_profile.operational.value}"
    )

    diffs_section = _build_diffs_section(files, file_diffs or {})

    return _AGENT_PROMPT_TEMPLATE.format(
        agent_name=spec.name.replace("_", " ").title(),
        diff_spec=pr_context.diff_spec,
        file_count=pr_context.file_count,
        total_lines=pr_context.total_changed_lines,
        risk_summary=risk_summary,
        file_list=file_list,
        focus_description=_FOCUS_DESCRIPTIONS.get(spec.name, "General code quality"),
        diffs_section=diffs_section,
    )


# ---------------------------------------------------------------------------
# Finding extraction
# ---------------------------------------------------------------------------

# Match a JSON array in the agent's answer
_JSON_ARRAY_RE = re.compile(r"\[[\s\S]*?\](?=\s*(?:```|$))", re.MULTILINE)


def _parse_findings(answer: str, spec: AgentSpec) -> List[ReviewFinding]:
    """Extract structured findings from an agent's answer text.

    The agent is prompted to output a JSON array. We extract it
    with regex to handle cases where the agent wraps it in markdown.
    """
    findings: List[ReviewFinding] = []

    # Try to find a JSON array in the answer
    match = _JSON_ARRAY_RE.search(answer)
    if not match:
        return findings

    try:
        raw_findings = json.loads(match.group())
    except json.JSONDecodeError:
        logger.warning("Failed to parse findings JSON from %s agent", spec.name)
        return findings

    if not isinstance(raw_findings, list):
        return findings

    severity_map = {
        "critical": Severity.CRITICAL,
        "warning": Severity.WARNING,
        "nit": Severity.NIT,
        "praise": Severity.PRAISE,
    }

    for raw in raw_findings:
        if not isinstance(raw, dict):
            continue
        severity_str = str(raw.get("severity", "warning")).lower()
        findings.append(ReviewFinding(
            title=raw.get("title", "Untitled finding"),
            category=spec.category,
            severity=severity_map.get(severity_str, Severity.WARNING),
            confidence=float(raw.get("confidence", 0.7)),
            file=raw.get("file", ""),
            start_line=int(raw.get("start_line", 0)),
            end_line=int(raw.get("end_line", 0)),
            evidence=raw.get("evidence", []),
            risk=raw.get("risk", ""),
            suggested_fix=raw.get("suggested_fix", ""),
            agent=spec.name,
        ))

    return findings


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
    llm_semaphore: Optional["asyncio.Semaphore"] = None,
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
    query = _build_agent_query(spec, pr_context, risk_profile, file_diffs)

    budget = BudgetConfig(
        max_input_tokens=spec.budget_tokens,
        max_iterations=spec.max_iterations,
    )

    agent = AgentLoopService(
        provider=provider,
        max_iterations=spec.max_iterations,
        budget_config=budget,
        trace_writer=trace_writer,
        _skip_review_delegation=True,
        llm_semaphore=llm_semaphore,
    )

    try:
        result: AgentResult = await agent.run(
            query=query,
            workspace_path=workspace_path,
        )

        findings = _parse_findings(result.answer, spec)

        tokens = 0
        if result.budget_summary:
            tokens = result.budget_summary.get("total_input_tokens", 0) + \
                     result.budget_summary.get("total_output_tokens", 0)

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
