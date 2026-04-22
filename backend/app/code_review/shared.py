"""Shared utilities for PR review.

Deterministic helpers consumed by ``PRBrainOrchestrator`` v2 coordinator:
  * ``parse_findings`` — JSON-fenced / raw finding extraction from LLM output
  * ``parse_findings_with_status`` — variant that preserves worker status tags
  * ``build_diffs_section`` — PR diff formatting for sub-agent prompts
  * ``FOCUS_DESCRIPTIONS`` — risk-lens focus hints
  * ``evidence_gate`` — per-finding confidence/evidence threshold filter
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
from typing import Dict, List, Optional, Tuple

from app.ai_provider.base import AIProvider

from .models import (
    ChangedFile,
    FindingCategory,
    PRContext,
    ReviewFinding,
    RiskProfile,
    Severity,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Minimum confidence to keep a finding (below this = too speculative).
MIN_CONFIDENCE = 0.80  # raised from 0.75 to reduce weak/speculative findings

# Matches "diff --git a/path b/path" headers in unified diff output
DIFF_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)$", re.MULTILINE)

# Max chars per individual file diff before truncation.
MAX_FILE_DIFF_CHARS = 15_000
# Max total chars of diffs injected into an agent prompt.
MAX_TOTAL_DIFF_CHARS = 120_000

# Minimum requirements for a Critical finding to keep its severity
CRITICAL_MIN_EVIDENCE = 2
CRITICAL_REQUIRE_FILE = True
CRITICAL_REQUIRE_LINE = True

# Max chars of agent answer fed to the repair LLM call (keep the prompt short and cheap).
_MAX_REPAIR_INPUT_CHARS = 3000

# Regex patterns for JSON extraction (ordered by specificity)
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\[[\s\S]*?\])\s*```")
_JSON_BARE_RE = re.compile(r"\[[\s\S]*\]")
_JSON_OBJECT_RE = re.compile(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}")

# Map raw severity strings from LLM output → Severity enum.
# "warning" remains a valid enum value (Severity.WARNING) for backward compat.
# The 4-level rubric encourages agents to output "high"/"medium"/"low" instead,
# but "warning" is still parsed and treated as ≈ MEDIUM in merge_recommendation.
_SEVERITY_MAP = {
    "critical": Severity.CRITICAL,
    "high": Severity.HIGH,
    "medium": Severity.MEDIUM,
    "low": Severity.LOW,
    "warning": Severity.WARNING,
    "nit": Severity.NIT,
    "praise": Severity.PRAISE,
}

# Per-agent focus descriptions — what each review dimension looks for
FOCUS_DESCRIPTIONS = {
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
    "performance": (
        "N+1 queries, unbounded loops/collections, missing pagination, "
        "large allocations in hot paths, repeated expensive work that could "
        "be memoized, chatty RPC sequences, sync calls on async paths."
    ),
    "correctness_b": (
        "Null safety, error handling, edge cases, and contract violations. "
        "For each changed method, trace every nullable return value and verify "
        "the caller handles null. Check exception paths for state consistency."
    ),
    "test_coverage": (
        "New logic without test coverage, untested failure paths, "
        "tests that don't assert meaningful behavior, missing edge case tests, "
        "untested concurrent/async paths."
    ),
}

# Per-agent strategy hints — HOW to investigate
STRATEGY_HINTS = {
    "correctness": (
        "Mixed strategy: scan all diffs for suspicious patterns first, "
        "then deep-dive the top 2-3 suspects with trace_variable and get_callees. "
        "Use git_show to compare code BEFORE vs AFTER the change. "
        "Budget 3-4 tool calls for scanning, 6-8 for deep investigation."
    ),
    "concurrency": (
        "Depth-first: identify shared-state operations, then trace each "
        "to check atomicity. Use ast_search for check-then-act patterns. "
        "Spend most tool calls proving or disproving one race at a time."
    ),
    "security": (
        "Depth-first: trace data from external input (HTTP, queue, file) "
        "through to storage/output. Use trace_variable for taint analysis. "
        "Use git_log search= to find related security fixes or CVEs. "
        "For each flow, verify sanitization/validation at every boundary."
    ),
    "reliability": (
        "Breadth-first: check every exception handler, resource acquisition, "
        "and error path in the changed files. Use get_callers to verify "
        "callers handle errors. Brief checks across many paths > deep dive on one."
    ),
    "performance": (
        "Scan diff for loops, list endpoints, and DB calls first. For each "
        "suspect, verify the input bound (is the collection user-controlled?) "
        "and whether the expensive work runs once or per-iteration. Use "
        "db_schema to confirm missing indexes before flagging slow queries. "
        "Prove scalability problems from code structure — avoid microbench speculation."
    ),
    "correctness_b": (
        "Defensive review: for each changed method, trace every nullable "
        "return value and verify the caller handles null. Check exception "
        "paths — does every catch block leave the system consistent? "
        "Use get_dependents to find callers that may break."
    ),
    "test_coverage": (
        "Breadth-first: for each changed file, use find_tests to locate "
        "existing tests. Use test_outline on found test files to assess "
        "coverage quality. Use run_test to execute key tests and verify "
        "they still pass. Focus on untested critical paths, not line counts."
    ),
}

# Agent category mapping (agent name → FindingCategory)
AGENT_CATEGORIES = {
    "correctness": FindingCategory.CORRECTNESS,
    "correctness_b": FindingCategory.CORRECTNESS,
    "concurrency": FindingCategory.CONCURRENCY,
    "security": FindingCategory.SECURITY,
    "reliability": FindingCategory.RELIABILITY,
    "performance": FindingCategory.PERFORMANCE,
    "test_coverage": FindingCategory.TEST_COVERAGE,
}

# Repair prompt for unparseable agent output
_REPAIR_PROMPT = """\
The following text was produced by a code review agent, but it is NOT valid JSON.
Extract the findings and reformat them as a JSON array.

## Rules
- Output ONLY a JSON array. No commentary, no markdown fences, no explanation.
- Each element must have: title, severity, confidence, file, start_line, end_line, evidence, risk, suggested_fix
- severity must be one of: "critical", "high", "medium", "low", "nit", "praise"
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


# ---------------------------------------------------------------------------
# Pre-computation helpers (from service.py)
# ---------------------------------------------------------------------------


def build_impact_context(
    workspace_path: str,
    pr_context: PRContext,
) -> str:
    """Query the dependency graph for callers/dependents of changed files.

    Returns a structured text block that can be injected into agent prompts
    so they see cross-file impact without burning tool-call budget.

    Args:
        workspace_path: Absolute path to the repo root.
        pr_context: Parsed PR metadata; only business-logic files are queried.

    Returns:
        Formatted markdown text describing caller (←) and dependency (→)
        relationships for up to 15 changed business-logic files, or an
        empty string if no dependency data is available.
    """
    try:
        from app.code_tools.tools import get_dependencies, get_dependents
    except ImportError:
        logger.warning("Impact graph unavailable: cannot import code_tools")
        return ""

    biz_files = pr_context.business_logic_files()
    if not biz_files:
        return ""

    sections: List[str] = []
    files_processed = 0

    for f in biz_files[:15]:  # cap to avoid slow scans on huge PRs
        dependents_result = get_dependents(workspace=workspace_path, file_path=f.path)
        dependencies_result = get_dependencies(workspace=workspace_path, file_path=f.path)

        dep_lines: List[str] = []

        if dependents_result.success and dependents_result.data:
            callers = dependents_result.data[:5]  # top 5 by weight
            caller_strs = [f"  ← {d['file_path']} (refs: {', '.join(d.get('symbols', [])[:3])})" for d in callers]
            dep_lines.extend(caller_strs)

        if dependencies_result.success and dependencies_result.data:
            deps = dependencies_result.data[:5]
            dep_strs = [f"  → {d['file_path']} (uses: {', '.join(d.get('symbols', [])[:3])})" for d in deps]
            dep_lines.extend(dep_strs)

        if dep_lines:
            sections.append(f"`{f.path}` (+{f.additions}/-{f.deletions}):\n" + "\n".join(dep_lines))
            files_processed += 1

    if not sections:
        return ""

    logger.info("Impact graph: computed dependencies for %d/%d files", files_processed, len(biz_files))
    return "## Impact Graph — callers (←) and dependencies (→) of changed files\n\n" + "\n\n".join(sections)


def extract_relevant_diff(full_diff: str, start_line: int, window: int = 80) -> str:
    """Extract the diff hunk(s) most relevant to a finding's line range.

    Instead of blindly truncating at N chars, finds the hunk containing
    ``start_line`` and returns a window around it.  Falls back to the first
    ``window`` lines if no matching hunk is found.

    Args:
        full_diff: Raw unified diff text for a single file.
        start_line: New-file line number that the finding references.
        window: Number of diff lines to include around the matching hunk.

    Returns:
        Relevant diff slice as a string.
    """
    if not full_diff or not start_line:
        lines = full_diff.split("\n")
        return "\n".join(lines[:window])

    lines = full_diff.split("\n")
    hunk_header_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

    best_start = 0
    for i, line in enumerate(lines):
        m = hunk_header_re.match(line)
        if m:
            hunk_start = int(m.group(1))
            hunk_len = int(m.group(2)) if m.group(2) else 1
            if hunk_start <= start_line <= hunk_start + hunk_len + 20:
                begin = max(0, i - 5)
                end = min(len(lines), i + window)
                return "\n".join(lines[begin:end])
            best_start = i

    if best_start > 0:
        begin = max(0, best_start - 5)
        end = min(len(lines), best_start + window)
        return "\n".join(lines[begin:end])

    return "\n".join(lines[:window])


def is_multi_source(finding: ReviewFinding) -> bool:
    """Check if a finding was reported by 2+ independent agents (dedup merges with '+')."""
    return "+" in finding.agent


def compute_budget_multiplier(pr_context: PRContext) -> float:
    """Compute a budget multiplier based on PR size.

    Tiers:
      <500 lines: 0.5× (quick review)
      500-2000 lines: 1.0× (standard)
      2000-5000 lines: 1.5×
      5000+ lines: 2.0×

    Args:
        pr_context: Parsed PR metadata; uses ``total_changed_lines``.

    Returns:
        A float multiplier to apply to each agent's base token budget.
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


_DEFAULT_REJECT_ABOVE = 6000


def should_reject_pr(
    pr_context: PRContext,
    max_lines: int = _DEFAULT_REJECT_ABOVE,
) -> Optional[str]:
    """Check if a PR is too large to review meaningfully.

    Args:
        pr_context: Parsed PR metadata used to check ``total_changed_lines``.
        max_lines: Maximum allowed changed lines. Defaults to 6 000.

    Returns:
        A human-readable rejection message when the PR exceeds the threshold,
        or ``None`` if the PR is within reviewable limits.
    """
    if pr_context.total_changed_lines > max_lines:
        return (
            f"This PR has {pr_context.total_changed_lines:,} lines of changes "
            f"across {pr_context.file_count} files, which is too large for an "
            f"effective review. Please split it into smaller PRs (ideally < 500 "
            f"lines each).\n\nChanged files:\n"
            + "\n".join(f"- `{f.path}` (+{f.additions}/-{f.deletions})" for f in pr_context.files[:30])
        )
    return None


def prefetch_diffs(workspace_path: str, diff_spec: str) -> Dict[str, str]:
    """Fetch all file diffs in a single ``git diff`` call and split by file.

    Returns a mapping of ``file_path → diff_text`` so that each review
    agent receives only the diffs relevant to its scope, without making
    redundant ``git_diff`` / ``git_diff_files`` tool calls.

    Args:
        workspace_path: Absolute path to the git repository root.
        diff_spec: Git diff spec passed verbatim (e.g. ``"HEAD~1..HEAD"``).

    Returns:
        Dict mapping each changed file path to its unified-diff text.
        Returns an empty dict on any git error.
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
    parts = DIFF_HEADER_RE.split(full_diff)

    for i in range(1, len(parts) - 2, 3):
        a_path = parts[i]
        b_path = parts[i + 1]
        body = parts[i + 2]
        header = f"diff --git a/{a_path} b/{b_path}"
        diffs[b_path] = header + body

    logger.info("Pre-fetched diffs for %d files", len(diffs))
    return diffs


# ---------------------------------------------------------------------------
# Post-processing helpers (from service.py + agents.py)
# ---------------------------------------------------------------------------


def post_filter(findings: list[ReviewFinding]) -> list[ReviewFinding]:
    """Apply quality rules to raw agent findings.

    Rules applied in order:
      1. Drop findings with confidence < MIN_CONFIDENCE (0.75).
      2. Test-coverage findings are capped at Warning — never Critical.
      3. Findings whose title contains "missing test" are capped at Warning.

    Args:
        findings: Raw findings from one or more review agents.

    Returns:
        Filtered list with low-confidence findings removed and severity
        caps applied.
    """
    result: list[ReviewFinding] = []
    dropped = 0

    for f in findings:
        if f.confidence < MIN_CONFIDENCE:
            dropped += 1
            continue

        # "Missing tests" is never critical — cap at high (provable but functional,
        # not security/contract).
        if f.category == FindingCategory.TEST_COVERAGE and f.severity == Severity.CRITICAL:
            f.severity = Severity.HIGH

        if "missing test" in f.title.lower() and f.severity == Severity.CRITICAL:
            f.severity = Severity.HIGH

        result.append(f)

    if dropped:
        logger.info("Post-filter: dropped %d low-confidence findings", dropped)
    return result


def merge_recommendation(findings: list) -> str:
    """Determine the merge recommendation based on findings severity.

    Logic: any Critical/High → request_changes; 3+ Medium → request_changes;
    1-2 Medium → approve_with_followups; no issues → approve.

    Args:
        findings: List of ReviewFinding (typically post-processed and ranked).

    Returns:
        One of: ``"approve"``, ``"approve_with_followups"``,
        ``"request_changes"``.
    """
    critical = sum(1 for f in findings if f.severity in (Severity.CRITICAL, Severity.HIGH))
    warnings = sum(1 for f in findings if f.severity in (Severity.MEDIUM, Severity.WARNING))

    if critical > 0:
        return "request_changes"
    if warnings >= 3:
        return "request_changes"
    if warnings > 0:
        return "approve_with_followups"
    return "approve"


def build_summary(
    pr_context: PRContext,
    risk_profile: RiskProfile,
    findings: list,
    merge_rec: str,
) -> str:
    """Build a human-readable review summary (fallback when LLM synthesis fails).

    Args:
        pr_context: Parsed PR metadata (diff spec, file count, line counts).
        risk_profile: Risk classification used to show the max risk level.
        findings: Post-processed ReviewFinding list; counts by severity.
        merge_rec: Merge recommendation string from ``merge_recommendation()``.

    Returns:
        Markdown-formatted summary string.
    """
    critical = sum(1 for f in findings if f.severity == Severity.CRITICAL)
    high = sum(1 for f in findings if f.severity == Severity.HIGH)
    medium = sum(1 for f in findings if f.severity in (Severity.MEDIUM, Severity.WARNING))
    low = sum(1 for f in findings if f.severity == Severity.LOW)
    nits = sum(1 for f in findings if f.severity == Severity.NIT)

    rec_emoji = {
        "approve": "Approve",
        "request_changes": "Request Changes",
        "approve_with_followups": "Approve (with follow-ups)",
    }

    lines = [
        f"## Code Review: {pr_context.diff_spec}",
        "",
        f"**{pr_context.file_count} files** | "
        f"**+{pr_context.total_additions}/-{pr_context.total_deletions} lines** | "
        f"Risk: {risk_profile.max_risk().value}",
        "",
        f"### Recommendation: {rec_emoji.get(merge_rec, merge_rec)}",
        "",
    ]

    if critical + high + medium + low + nits == 0:
        lines.append("No issues found. Code looks good!")
    else:
        if critical:
            lines.append(f"- **{critical} critical** issue(s)")
        if high:
            lines.append(f"- **{high} high** issue(s)")
        if medium:
            lines.append(f"- **{medium} medium** issue(s)")
        if low:
            lines.append(f"- {low} low issue(s)")
        if nits:
            lines.append(f"- {nits} nit(s)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Diff section builder (from agents.py)
# ---------------------------------------------------------------------------


def build_diffs_section(
    files: List[ChangedFile],
    file_diffs: Dict[str, str],
) -> str:
    """Build the pre-fetched diffs section for an agent prompt.

    Includes diffs for the agent's scoped files, truncating per-file diffs
    at MAX_FILE_DIFF_CHARS and capping the total at MAX_TOTAL_DIFF_CHARS to
    avoid blowing up the context window.

    Args:
        files: Scoped list of changed files for the requesting agent.
        file_diffs: Pre-fetched mapping of file path to diff text.

    Returns:
        Formatted string with fenced diff blocks, ready for prompt injection.
        Returns a fallback message when no diffs are available.
    """
    if not file_diffs:
        return "(diffs not available — use git_diff to fetch as needed)"

    sections = []
    total_chars = 0

    for f in files[:20]:
        diff_text = file_diffs.get(f.path, "")
        if not diff_text:
            continue

        if len(diff_text) > MAX_FILE_DIFF_CHARS:
            diff_text = (
                diff_text[:MAX_FILE_DIFF_CHARS]
                + f"\n... (truncated, {len(file_diffs[f.path]):,} chars total — use read_file for full content)"
            )

        if total_chars + len(diff_text) > MAX_TOTAL_DIFF_CHARS:
            remaining = len(files) - len(sections)
            sections.append(f"\n... ({remaining} more file(s) omitted — use git_diff to view)")
            break

        sections.append(f"### `{f.path}`\n```diff\n{diff_text}\n```")
        total_chars += len(diff_text)

    if not sections:
        return "(no diffs available for files in scope)"

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Finding extraction (from agents.py)
# ---------------------------------------------------------------------------


def raw_to_finding(
    raw: dict,
    agent_name: str,
    category: FindingCategory,
) -> Optional[ReviewFinding]:
    """Convert a raw LLM-output dict to a ReviewFinding, returning None if invalid.

    Args:
        raw: Parsed dict expected to contain title, severity, confidence,
            file, start_line, end_line, evidence, risk, and suggested_fix.
        agent_name: Name of the agent that produced this finding (for attribution).
        category: Finding category to assign (e.g. CORRECTNESS, SECURITY).

    Returns:
        A ReviewFinding instance, or None if the dict is missing required
        fields or cannot be coerced to the correct types.
    """
    if not isinstance(raw, dict):
        return None
    if not raw.get("title") and not raw.get("file"):
        return None
    severity_str = str(raw.get("severity", "warning")).lower()
    try:
        return ReviewFinding(
            title=raw.get("title", "Untitled finding"),
            category=category,
            severity=_SEVERITY_MAP.get(severity_str, Severity.WARNING),
            confidence=float(raw.get("confidence", 0.7)),
            file=raw.get("file", ""),
            start_line=int(raw.get("start_line", 0)),
            end_line=int(raw.get("end_line", 0)),
            evidence=_normalize_evidence(raw.get("evidence")),
            risk=raw.get("risk", ""),
            suggested_fix=raw.get("suggested_fix", ""),
            agent=agent_name,
            reasoning=raw.get("reasoning", ""),
        )
    except (TypeError, ValueError):
        return None


def _normalize_evidence(raw) -> List[str]:
    """Coerce the LLM's ``evidence`` field into a clean ``List[str]``.

    The LLM is supposed to emit a JSON array of strings. Real observed
    malformations:

    * ``None`` or missing — treat as no evidence.
    * Plain string (``"cache key missing user_id at line 45"``) — LLM
      emitted a single sentence rather than an array. Wrap in a
      single-element list.
    * Re-serialised string-as-list (``["@", "V", "a", "l", "u", "e", ...]``)
      — happens when a JSON string got round-tripped through a parser
      that applied ``list(s)`` to an inner string. The rendered markdown
      shows one character per bullet (observed on ADO PR #14227).
      Heuristic: ≥ 6 items AND every item is a single character → rejoin.
    * Array with non-string elements — stringify the stringy ones
      (``int``/``float`` acceptable), drop the rest (``None``, dicts,
      nested lists).

    Fail-safe: on any unexpected shape return ``[]`` rather than raise.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        stripped = raw.strip()
        return [stripped] if stripped else []
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    for item in raw:
        if isinstance(item, str):
            if item.strip():
                out.append(item.strip())
        elif isinstance(item, (int, float, bool)):
            out.append(str(item))
        # silently drop dict / None / nested list
    # Character-list heuristic — a string that got ``list()``-unpacked
    # upstream produces a fragmented evidence list. Signs: ≥ 6 items
    # AND at least 70% of items are 1 character long (real-world
    # malformed evidence shows up as a mix of single chars + a few
    # 2-10 char fragments). Rejoin with no separator (the original
    # string's whitespace lives inside the characters). Threshold is
    # intentionally below 100% because observed shapes look like
    # ``['@Value("', 'a', 'b', 'o', 'u', 'n', 'd', '")']`` = 75%
    # single-char items, and those ARE the fragmented shape we want
    # to rejoin.
    if len(out) >= 6:
        single_char_ratio = sum(1 for s in out if len(s) == 1) / len(out)
        if single_char_ratio >= 0.7:
            rejoined = "".join(out).strip()
            return [rejoined] if rejoined else []
    return out


def _try_parse_json_array(text: str) -> Optional[list]:
    """Attempt to parse *text* as a JSON array, return None on failure."""
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def parse_findings_with_status(
    answer: str,
    agent_name: str,
    category: FindingCategory,
    *,
    warn_on_empty: bool = True,
) -> Tuple[List[ReviewFinding], bool]:
    """Extract structured findings from an agent's answer text.

    Tries multiple extraction strategies in priority order:
      1. JSON array inside a fenced ``json`` code block.
      2. Bare JSON array anywhere in the text.
      3. Individual JSON objects (for models that omit the array wrapper).

    Returns a (findings, parsed_explicit_array) tuple. The boolean is True
    when we found and successfully parsed an explicit JSON array — even if
    it was empty (``[]``). Empty-array is the agent's authoritative way to
    say "no findings to report" (per the prompt: ``If you find no issues,
    output exactly: `[]```), and callers should NOT trigger a repair retry
    in that case.

    Args:
        answer: Raw text produced by the review agent.
        agent_name: Agent name attributed to each parsed finding.
        category: Finding category assigned to each parsed finding.
        warn_on_empty: When True, logs a warning if neither an explicit
            array nor any individual finding objects could be parsed.

    Returns:
        Tuple of (list of valid ReviewFinding, parsed_explicit_array bool).
    """
    findings: List[ReviewFinding] = []

    # Strategy 1: JSON array inside a ```json code block. An array here is
    # authoritative when EITHER it's explicitly empty (the agent's "no
    # findings" answer) OR it produced at least one valid finding. A
    # non-empty array that yielded zero findings is most likely an embedded
    # data structure in prose, not the answer — fall through to other
    # strategies in that case.
    for m in _JSON_BLOCK_RE.finditer(answer):
        raw_list = _try_parse_json_array(m.group(1))
        if raw_list is None:
            continue
        block_findings: List[ReviewFinding] = []
        for raw in raw_list:
            f = raw_to_finding(raw, agent_name, category)
            if f:
                block_findings.append(f)
        if not raw_list or block_findings:
            findings.extend(block_findings)
            return findings, True

    # Strategy 2: bare JSON array (no fence). Same authority rule — only
    # short-circuit on an explicit empty array or a populated array that
    # produced findings. Skip junk arrays embedded in prose (e.g. an
    # ``evidence: ["..."]`` field of an enclosing object).
    for m in _JSON_BARE_RE.finditer(answer):
        raw_list = _try_parse_json_array(m.group())
        if raw_list is None:
            continue
        bare_findings: List[ReviewFinding] = []
        for raw in raw_list:
            f = raw_to_finding(raw, agent_name, category)
            if f:
                bare_findings.append(f)
        if not raw_list or bare_findings:
            findings.extend(bare_findings)
            return findings, True

    # Strategy 3: individual JSON objects scattered through prose. This is
    # a recovery path — there's no explicit array marker, so we cannot
    # treat absence of objects as an authoritative empty answer.
    for m in _JSON_OBJECT_RE.finditer(answer):
        try:
            raw = json.loads(m.group())
            f = raw_to_finding(raw, agent_name, category)
            if f:
                findings.append(f)
        except (json.JSONDecodeError, ValueError):
            continue

    if findings:
        logger.info(
            "Parsed %d findings from %s agent via individual JSON objects",
            len(findings),
            agent_name,
        )
        return findings, False  # recovered objects, not an explicit array

    if warn_on_empty:
        logger.warning("Failed to parse findings JSON from %s agent", agent_name)

    return findings, False


def parse_findings(
    answer: str,
    agent_name: str,
    category: FindingCategory,
    *,
    warn_on_empty: bool = True,
) -> List[ReviewFinding]:
    """Backwards-compatible wrapper around :func:`parse_findings_with_status`.

    Drops the parsed-explicit-array flag and returns just the findings list.
    New callers should prefer ``parse_findings_with_status`` so they can
    distinguish a real parse failure from a legitimate empty answer.
    """
    findings, _ = parse_findings_with_status(
        answer, agent_name, category, warn_on_empty=warn_on_empty
    )
    return findings


async def repair_output(
    answer: str,
    agent_name: str,
    category: FindingCategory,
    provider: AIProvider,
) -> List[ReviewFinding]:
    """Attempt to recover findings by asking the model to reformat the answer.

    Called when the agent produced non-empty text but ``parse_findings``
    could not extract valid JSON.  Makes one additional LLM call with a
    short, cheap prompt to extract and reformat the findings as JSON.

    Args:
        answer: Non-JSON text produced by the review agent.
        agent_name: Agent name attributed to any recovered findings.
        category: Finding category assigned to each recovered finding.
        provider: LLM provider used for the repair call.

    Returns:
        List of recovered ReviewFinding objects, or an empty list if the
        repair call fails or produces no parseable findings.
    """
    prompt = _REPAIR_PROMPT.format(answer=answer[:_MAX_REPAIR_INPUT_CHARS])
    try:
        loop = asyncio.get_event_loop()
        repaired = await loop.run_in_executor(
            None,
            lambda: provider.call_model(prompt=prompt, max_tokens=2048, assistant_prefix="["),
        )
        findings = parse_findings(repaired, agent_name, category, warn_on_empty=False)
        if findings:
            logger.info(
                "Repair loop recovered %d findings for %s agent",
                len(findings),
                agent_name,
            )
        return findings
    except Exception as exc:
        logger.warning("Repair loop failed for %s agent: %s", agent_name, exc)
        return []


def evidence_gate(findings: List[ReviewFinding], tool_calls_made: int = 0) -> List[ReviewFinding]:
    """Validate evidence quality for Critical findings.

    Critical findings must meet a minimum evidence bar:
      1. At least ``CRITICAL_MIN_EVIDENCE`` evidence strings.
      2. Must reference a specific file.
      3. Must reference a specific line number.
      4. Agent must have made ≥3 tool calls (i.e. actually investigated).

    Findings that fail are downgraded to Warning with an explanatory note
    appended to their evidence list.

    Args:
        findings: List of findings to validate (all severities accepted;
            non-Critical findings pass through unchanged).
        tool_calls_made: Number of tool calls made by the producing agent,
            used to verify the agent actually investigated before reporting.

    Returns:
        The same list with under-evidenced Critical findings downgraded to
        Warning severity.
    """
    gated: List[ReviewFinding] = []
    for f in findings:
        if f.severity != Severity.CRITICAL:
            gated.append(f)
            continue

        reasons: List[str] = []

        if len(f.evidence) < CRITICAL_MIN_EVIDENCE:
            reasons.append(f"only {len(f.evidence)} evidence items (need {CRITICAL_MIN_EVIDENCE})")
        if CRITICAL_REQUIRE_FILE and not f.file:
            reasons.append("no file reference")
        if CRITICAL_REQUIRE_LINE and f.start_line == 0:
            reasons.append("no line number")
        if tool_calls_made < 3:
            reasons.append(f"only {tool_calls_made} tool calls (need ≥3)")

        if reasons:
            logger.info(
                "Evidence gate: downgrading '%s' from critical → high (%s)",
                f.title,
                "; ".join(reasons),
            )
            f.severity = Severity.HIGH
            f.evidence.append(f"[auto-downgraded: {'; '.join(reasons)}]")

        gated.append(f)

    return gated
