"""Specialized review agent definitions.

Each agent has a focused prompt, tool set, and budget tailored to its
review dimension. The orchestrator (CodeReviewService) dispatches them
in parallel and merges their findings.
"""
from __future__ import annotations

import asyncio
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
    strategy_hint: str = ""           # per-agent investigation strategy

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
_MAIN_TOKENS: int = 800_000   # mirrors AgentLoopService / BudgetConfig default
_MAIN_ITERS:  int = 40        # mirrors AgentLoopService default
_SUB_FRACTION: float = 0.7    # sub-agents get 70% of the main budget


def _sub_budget(weight: float) -> int:
    return int(_MAIN_TOKENS * _SUB_FRACTION * weight)


def _sub_iters(weight: float) -> int:
    return max(int(_MAIN_ITERS * _SUB_FRACTION * weight), 8)


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
        budget_tokens=_sub_budget(1.00),   # 560,000
        max_iterations=_sub_iters(1.00),   # 28
        risk_dimensions=["correctness"],
        strategy_hint=(
            "Mixed strategy: scan all diffs for suspicious patterns first, "
            "then deep-dive the top 2-3 suspects with trace_variable and get_callees. "
            "Budget 3-4 tool calls for scanning, 6-8 for deep investigation."
        ),
    ),
    AgentSpec(
        name="concurrency",
        category=FindingCategory.CONCURRENCY,
        tools=_REVIEW_CORE_TOOLS + [
            "git_diff", "find_references",
            "get_callers", "get_callees", "trace_variable",
            "ast_search",
        ],
        budget_tokens=_sub_budget(0.85),   # 476,000
        max_iterations=_sub_iters(0.85),   # 23
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
        tools=_REVIEW_CORE_TOOLS + [
            "git_diff", "trace_variable",
            "find_references", "git_blame", "ast_search",
        ],
        budget_tokens=_sub_budget(0.75),   # 420,000
        max_iterations=_sub_iters(0.75),   # 21
        risk_dimensions=["security"],
        strategy_hint=(
            "Depth-first: trace data from external input (HTTP, queue, file) "
            "through to storage/output. Use trace_variable for taint analysis. "
            "For each flow, verify sanitization/validation at every boundary."
        ),
    ),
    AgentSpec(
        name="reliability",
        category=FindingCategory.RELIABILITY,
        tools=_REVIEW_CORE_TOOLS + [
            "git_diff", "get_callers",
            "find_references", "git_log", "git_show",
        ],
        budget_tokens=_sub_budget(0.70),   # 392,000
        max_iterations=_sub_iters(0.70),   # 19
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
        tools=_REVIEW_CORE_TOOLS + [
            "git_diff", "find_tests",
            "test_outline", "find_references", "list_files",
        ],
        budget_tokens=_sub_budget(0.55),   # 308,000
        max_iterations=_sub_iters(0.55),   # 15
        risk_dimensions=[],   # always runs (via always_run flag)
        strategy_hint=(
            "Breadth-first: for each changed file, use find_tests to locate "
            "existing tests. Use test_outline on found test files to assess "
            "coverage quality. Focus on untested critical paths, not line counts."
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
3. Use additional tools (find_references, get_callers, trace_variable, etc.) to trace impact.
4. The file list and diffs are already provided — skip git_diff_files.
5. When you have enough evidence, stop investigating and produce your findings JSON.

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
# Finding extraction
# ---------------------------------------------------------------------------

# Regex patterns for JSON extraction (ordered by specificity)
# 1. JSON array inside a markdown code block
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\[[\s\S]*?\])\s*```")
# 2. Bare JSON array (greedy — find the longest valid array)
_JSON_BARE_RE = re.compile(r"\[[\s\S]*\]")
# 3. Individual JSON objects (fallback for models that forget the array wrapper)
_JSON_OBJECT_RE = re.compile(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}")

_SEVERITY_MAP = {
    "critical": Severity.CRITICAL,
    "warning": Severity.WARNING,
    "nit": Severity.NIT,
    "praise": Severity.PRAISE,
}


def _raw_to_finding(raw: dict, spec: AgentSpec) -> Optional[ReviewFinding]:
    """Convert a raw dict to a ReviewFinding, or None if invalid."""
    if not isinstance(raw, dict):
        return None
    # Must have at least a title or file to be considered a finding
    if not raw.get("title") and not raw.get("file"):
        return None
    severity_str = str(raw.get("severity", "warning")).lower()
    try:
        return ReviewFinding(
            title=raw.get("title", "Untitled finding"),
            category=spec.category,
            severity=_SEVERITY_MAP.get(severity_str, Severity.WARNING),
            confidence=float(raw.get("confidence", 0.7)),
            file=raw.get("file", ""),
            start_line=int(raw.get("start_line", 0)),
            end_line=int(raw.get("end_line", 0)),
            evidence=raw.get("evidence", []),
            risk=raw.get("risk", ""),
            suggested_fix=raw.get("suggested_fix", ""),
            agent=spec.name,
            reasoning=raw.get("reasoning", ""),
        )
    except (TypeError, ValueError):
        return None


def _try_parse_json_array(text: str) -> Optional[list]:
    """Attempt to parse *text* as a JSON array, return None on failure."""
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def _parse_findings(
    answer: str,
    spec: AgentSpec,
    *,
    warn_on_empty: bool = True,
) -> List[ReviewFinding]:
    """Extract structured findings from an agent's answer text.

    Tries multiple extraction strategies (in order):
    1. JSON array inside a ```json ... ``` code block
    2. Bare JSON array anywhere in the text
    3. Individual JSON objects (for models that omit the array wrapper)

    This robustness is needed because Qwen Flash models sometimes produce
    slightly malformed output (e.g. commentary after the JSON, missing
    array brackets, or extra whitespace).

    Args:
        warn_on_empty: If *False*, suppress the "Failed to parse" warning.
            Used when parsing context chunks where missing JSON is expected.
    """
    findings: List[ReviewFinding] = []

    # Strategy 1: JSON in a markdown code block (most reliable)
    for m in _JSON_BLOCK_RE.finditer(answer):
        raw_list = _try_parse_json_array(m.group(1))
        if raw_list is not None:
            for raw in raw_list:
                f = _raw_to_finding(raw, spec)
                if f:
                    findings.append(f)
            if findings:
                return findings

    # Strategy 2: Bare JSON array (try all matches, pick the one with findings)
    for m in _JSON_BARE_RE.finditer(answer):
        raw_list = _try_parse_json_array(m.group())
        if raw_list is not None:
            for raw in raw_list:
                f = _raw_to_finding(raw, spec)
                if f:
                    findings.append(f)
            if findings:
                return findings

    # Strategy 3: Individual JSON objects (last resort)
    for m in _JSON_OBJECT_RE.finditer(answer):
        try:
            raw = json.loads(m.group())
            f = _raw_to_finding(raw, spec)
            if f:
                findings.append(f)
        except (json.JSONDecodeError, ValueError):
            continue

    if findings:
        logger.info(
            "Parsed %d findings from %s agent via individual JSON objects",
            len(findings), spec.name,
        )

    if not findings and warn_on_empty:
        logger.warning("Failed to parse findings JSON from %s agent", spec.name)

    return findings


# ---------------------------------------------------------------------------
# Output repair — ask model to reformat unparseable answer as JSON
# ---------------------------------------------------------------------------

_REPAIR_PROMPT = """\
The following text was produced by a code review agent, but it is NOT valid JSON.
Extract the findings and reformat them as a JSON array.

## Rules
- Output ONLY a JSON array. No commentary, no markdown fences, no explanation.
- Each element must have: title, severity, confidence, file, start_line, end_line, evidence, risk, suggested_fix
- severity must be one of: "critical", "warning", "nit", "praise"
- confidence must be a number 0.0–1.0
- evidence must be an array of strings
- If the text contains no reviewable findings, output: []

## Example

<input_text>
Looking at the code, I found two issues:
1. The cache key in utils.py line 45 doesn't include the user ID, so users can see each other's data. This is a serious security issue.
2. Minor: the variable name `x` on line 12 is unclear.
</input_text>

<expected_output>
[{{"title":"Cache key missing user ID allows cross-user data leak","severity":"critical","confidence":0.85,"file":"utils.py","start_line":45,"end_line":45,"evidence":["cache key at line 45 does not include user_id"],"risk":"Users can see other users cached data","suggested_fix":"Include user_id in cache key: cache_key = f'{{user_id}}:{{resource}}'"}},{{"title":"Unclear variable name","severity":"nit","confidence":0.9,"file":"utils.py","start_line":12,"end_line":12,"evidence":["variable named x at line 12"],"risk":"Reduced readability","suggested_fix":"Rename x to a descriptive name"}}]
</expected_output>

## Text to reformat
<input_text>
{answer}
</input_text>
"""


async def _repair_output(
    answer: str,
    spec: AgentSpec,
    provider: AIProvider,
) -> List[ReviewFinding]:
    """Attempt to recover findings by asking the model to reformat the answer.

    Called when the agent produced non-empty text but ``_parse_findings``
    could not extract valid JSON.  One extra LLM call (cheap — short prompt,
    short response).

    Returns:
        List of successfully parsed findings (may be empty).
    """
    prompt = _REPAIR_PROMPT.format(answer=answer[:3000])  # cap input
    try:
        loop = asyncio.get_event_loop()
        repaired = await loop.run_in_executor(
            None,
            lambda: provider.call_model(
                prompt=prompt, max_tokens=2048, assistant_prefix="["
            ),
        )
        findings = _parse_findings(repaired, spec, warn_on_empty=False)
        if findings:
            logger.info(
                "Repair loop recovered %d findings for %s agent",
                len(findings), spec.name,
            )
        return findings
    except Exception as exc:
        logger.warning("Repair loop failed for %s agent: %s", spec.name, exc)
        return []


# ---------------------------------------------------------------------------
# Evidence gate — downgrade under-evidenced Critical findings
# ---------------------------------------------------------------------------

# Minimum requirements for a Critical finding to keep its severity
_CRITICAL_MIN_EVIDENCE = 2       # at least 2 evidence items
_CRITICAL_REQUIRE_FILE = True    # must have file reference
_CRITICAL_REQUIRE_LINE = True    # must have start_line > 0


def _evidence_gate(findings: List[ReviewFinding], agent_result: "AgentResult") -> List[ReviewFinding]:
    """Validate evidence quality for Critical findings.

    Critical findings must meet a minimum evidence bar:
      1. At least ``_CRITICAL_MIN_EVIDENCE`` evidence strings.
      2. Must reference a specific file.
      3. Must reference a specific line number.
      4. Agent must have made ≥3 tool calls (i.e. actually investigated).

    Findings that fail are downgraded to Warning with a note.
    """
    tool_calls = agent_result.tool_calls_made if agent_result else 0

    gated: List[ReviewFinding] = []
    for f in findings:
        if f.severity != Severity.CRITICAL:
            gated.append(f)
            continue

        reasons: List[str] = []

        if len(f.evidence) < _CRITICAL_MIN_EVIDENCE:
            reasons.append(
                f"only {len(f.evidence)} evidence items (need {_CRITICAL_MIN_EVIDENCE})"
            )
        if _CRITICAL_REQUIRE_FILE and not f.file:
            reasons.append("no file reference")
        if _CRITICAL_REQUIRE_LINE and f.start_line == 0:
            reasons.append("no line number")
        if tool_calls < 3:
            reasons.append(f"only {tool_calls} tool calls (need ≥3)")

        if reasons:
            logger.info(
                "Evidence gate: downgrading '%s' from critical → warning (%s)",
                f.title, "; ".join(reasons),
            )
            f.severity = Severity.WARNING
            f.evidence.append(f"[auto-downgraded: {'; '.join(reasons)}]")

        gated.append(f)

    return gated


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
        _skip_review_delegation=True,
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
                    chunk.content, spec, warn_on_empty=False,
                )
                findings.extend(chunk_findings)
            if findings:
                logger.info(
                    "Recovered %d findings from context chunks for %s agent",
                    len(findings), spec.name,
                )

        # --- Improvement 1: Output Repair Loop ---
        # If no findings but the agent produced text, ask the model to
        # reformat the answer as JSON (one cheap extra call).
        if not findings and result.answer and len(result.answer) > 50:
            logger.info(
                "Agent '%s' produced %d chars but no parseable findings — "
                "attempting repair loop",
                spec.name, len(result.answer),
            )
            findings = await _repair_output(result.answer, spec, provider)

        if not findings and result.answer:
            logger.warning(
                "Agent '%s' produced %d chars of text but no parseable findings "
                "(even after repair). First 200 chars: %s",
                spec.name, len(result.answer), result.answer[:200],
            )

        # --- Improvement 3: Evidence Gate ---
        # Downgrade Critical findings that lack sufficient evidence.
        if findings:
            findings = _evidence_gate(findings, result)

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
