"""Query classifier — categorizes user questions to select optimal search strategy.

Two modes:
  * **Keyword matching** (default) — zero latency, no LLM cost.
  * **LLM classification** — uses a lightweight model (e.g. Haiku) for higher
    accuracy. Falls back to keyword matching on failure.

The classification result determines:
  * Which tools are included in the LLM tool set (dynamic tool selection)
  * Strategy hint appended to the user message
  * Token budget recommendation
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from app.ai_provider.base import AIProvider

logger = logging.getLogger(__name__)

# All tools available in the system
_ALL_TOOLS = [
    "grep", "read_file", "list_files", "find_symbol", "find_references",
    "file_outline", "get_dependencies", "get_dependents", "git_log",
    "git_diff", "git_diff_files", "ast_search", "get_callees", "get_callers",
    "git_blame", "git_show", "find_tests", "test_outline", "trace_variable",
    "compressed_view", "module_summary", "expand_symbol", "detect_patterns",
]

# Core tools always included regardless of query type
_CORE_TOOLS = [
    "grep", "read_file", "find_symbol", "file_outline",
    "compressed_view", "expand_symbol",
]


@dataclass
class QueryClassification:
    query_type: str
    strategy: str
    initial_tools: List[str]
    budget_level: str                # "low" | "medium" | "high"
    suggested_token_budget: int
    tool_set: List[str]              # dynamic tool set for this query type
    diff_spec: Optional[str] = None  # extracted git ref spec for code_review

    @property
    def is_high_level(self) -> bool:
        return self.query_type in ("architecture_question", "business_flow_tracing")


QUERY_TYPES: Dict[str, dict] = {
    "entry_point_discovery": {
        "description": "Find the entry point for a feature or endpoint",
        "strategy": "grep routes/endpoints → find_symbol handler → compressed_view → trace inward",
        "initial_tools": ["grep", "find_symbol"],
        "budget_level": "low",
        "suggested_token_budget": 200_000,
        "keywords": [
            "entry", "endpoint", "route", "handler",
            "where does", "where is", "which file",
        ],
        "tools": _CORE_TOOLS + [
            "find_references", "get_callees", "list_files",
        ],
    },
    "business_flow_tracing": {
        "description": "Trace a complete business process path",
        "strategy": "find entry → compressed_view → get_callees → trace_variable → map complete flow",
        "initial_tools": ["find_symbol", "get_callees", "trace_variable"],
        "budget_level": "medium",
        "suggested_token_budget": 400_000,
        "keywords": [
            "flow", "process", "trace", "how does", "what happens",
            "step by step", "lifecycle", "life cycle", "pipeline",
            "workflow", "journey", "sequence",
        ],
        "tools": _CORE_TOOLS + [
            "module_summary", "get_callees", "get_callers",
            "trace_variable", "get_dependencies", "list_files",
            "find_references", "detect_patterns",
        ],
    },
    "root_cause_analysis": {
        "description": "Analyze the root cause of a bug or error",
        "strategy": "grep error → find_references → get_callers → trace_variable → check git history",
        "initial_tools": ["grep", "find_references", "get_callers"],
        "budget_level": "high",
        "suggested_token_budget": 500_000,
        "keywords": [
            "bug", "error", "fail", "why", "root cause", "debug",
            "crash", "exception", "broken", "wrong", "issue",
        ],
        "tools": _CORE_TOOLS + [
            "find_references", "get_callers", "get_callees",
            "trace_variable", "git_log", "git_diff", "git_blame",
            "git_show", "find_tests", "detect_patterns",
        ],
    },
    "impact_analysis": {
        "description": "Assess the impact of modifying code",
        "strategy": "find_references → get_dependents → find_tests → compressed_view affected files",
        "initial_tools": ["get_dependents", "find_references", "find_tests"],
        "budget_level": "medium",
        "suggested_token_budget": 350_000,
        "keywords": [
            "impact", "affect", "break", "blast radius", "change",
            "modify", "refactor", "rename", "remove", "deprecate",
        ],
        "tools": _CORE_TOOLS + [
            "find_references", "get_dependents", "get_dependencies",
            "find_tests", "test_outline", "get_callers",
            "detect_patterns",
        ],
    },
    "architecture_question": {
        "description": "Understand overall architecture or module relationships",
        "strategy": "module_summary top dirs → get_dependencies → compressed_view key files",
        "initial_tools": ["list_files", "module_summary", "get_dependencies"],
        "budget_level": "medium",
        "suggested_token_budget": 300_000,
        "keywords": [
            "architecture", "structure", "organized", "overview",
            "design", "modules", "layers", "components", "diagram",
        ],
        "tools": _CORE_TOOLS + [
            "module_summary", "list_files", "get_dependencies",
            "get_dependents", "detect_patterns",
        ],
    },
    "config_analysis": {
        "description": "Understand the impact of configuration settings",
        "strategy": "grep config key → find_references → trace_variable → check consumers",
        "initial_tools": ["grep", "find_references", "trace_variable"],
        "budget_level": "low",
        "suggested_token_budget": 200_000,
        "keywords": [
            "config", "setting", "flag", "environment", "variable",
            "option", "parameter", "toggle", "feature flag",
        ],
        "tools": _CORE_TOOLS + [
            "find_references", "trace_variable", "list_files",
        ],
    },
    "data_lineage": {
        "description": "Trace how data flows through the system",
        "strategy": "find data source → trace_variable forward → find sinks → verify with read_file",
        "initial_tools": ["trace_variable", "find_references", "grep"],
        "budget_level": "high",
        "suggested_token_budget": 450_000,
        "keywords": [
            "data", "lineage", "transform", "input", "flows to",
            "passed to", "stored", "database", "persist", "column",
        ],
        "tools": _CORE_TOOLS + [
            "trace_variable", "find_references", "get_callees",
            "get_callers", "get_dependencies", "ast_search",
        ],
    },
    "code_review": {
        "description": "Review code changes in a PR or diff",
        "strategy": "git_diff_files → triage → git_diff per file → read context → check tests → summarize",
        "initial_tools": ["git_diff_files", "git_diff", "read_file"],
        "budget_level": "high",
        "suggested_token_budget": 600_000,
        "keywords": [
            "review", "code review", "pr review", "pull request",
            "review the pr", "review this pr", "review the diff",
            "review the changes", "review changes",
            "do pr", "do a pr", "check the pr",
        ],
        "tools": _CORE_TOOLS + [
            "git_diff_files", "git_diff", "git_log", "git_show",
            "git_blame", "find_references", "get_callers", "get_callees",
            "find_tests", "test_outline", "list_files", "detect_patterns",
        ],
    },
    "recent_changes": {
        "description": "Understand recent commits, diffs, or change history",
        "strategy": "git_log → git_show interesting commits → git_diff for details → read_file affected code",
        "initial_tools": ["git_log", "git_diff", "git_show"],
        "budget_level": "low",
        "suggested_token_budget": 250_000,
        "keywords": [
            "commit", "commits", "recent", "latest", "last change",
            "changed", "modified", "diff", "history", "log",
            "blame", "who changed", "who wrote", "when did",
            "what changed", "changelog", "merge", "merged",
            "pull request", "pr", "pushed", "reverted",
        ],
        "tools": _CORE_TOOLS + [
            "git_log", "git_diff", "git_blame", "git_show",
            "find_references", "list_files",
        ],
    },
}

# Deduplicate tool lists
for _spec in QUERY_TYPES.values():
    _spec["tools"] = list(dict.fromkeys(_spec["tools"]))


# ---------------------------------------------------------------------------
# PR / diff pattern detection — fires before keyword matching
# ---------------------------------------------------------------------------

# Matches patterns like:
#   "PR master...feature/xxx"
#   "do PR 'git diff master...feature'"
#   "review diff master..HEAD"
#   "code review HEAD~5"
#   "@AI do PR master...feature/branch-name"
_REF_CHARS = r'[a-zA-Z0-9_.~^/-]'
_PR_PATTERN = re.compile(
    r'(?:do\s+)?(?:pr|code\s*review|review\s+(?:the\s+)?(?:pr|diff|changes))'
    r'[\s:]*["\']?'
    r'(?:git\s+diff\s+)?'
    rf'({_REF_CHARS}+(?:\.{"{2,3}"}){_REF_CHARS}+)',
    re.IGNORECASE,
)
# Also match standalone diff specs: "master...feature/xxx"
_DIFF_SPEC_PATTERN = re.compile(
    rf'({_REF_CHARS}+\.{{2,3}}{_REF_CHARS}+)',
)


def _detect_pr_pattern(question: str) -> Optional[str]:
    """Detect a PR/diff pattern and extract the git ref spec.

    Returns the ref spec (e.g. 'master...feature/xxx') or None.
    """
    m = _PR_PATTERN.search(question)
    if m:
        return m.group(1)
    # Fallback: if the query contains "review"/"PR" and a diff spec
    q_lower = question.lower()
    if any(kw in q_lower for kw in ("review", "pr", "审核", "审查")):
        m = _DIFF_SPEC_PATTERN.search(question)
        if m:
            return m.group(1)
    return None


def classify_query(question: str) -> QueryClassification:
    """Classify a user question using keyword matching (zero latency).

    Returns a QueryClassification with the best-match query type,
    strategy hint, suggested initial tools, and dynamic tool set.
    """
    # Fast-path: PR/diff pattern detection
    diff_ref = _detect_pr_pattern(question)
    if diff_ref:
        spec = QUERY_TYPES["code_review"]
        logger.info("PR pattern detected: ref='%s'", diff_ref)
        return QueryClassification(
            query_type="code_review",
            strategy=spec["strategy"],
            initial_tools=spec["initial_tools"],
            budget_level=spec["budget_level"],
            suggested_token_budget=spec["suggested_token_budget"],
            tool_set=spec["tools"],
            diff_spec=diff_ref,
        )

    q_lower = question.lower()
    best_type = "business_flow_tracing"  # safe default
    best_score = 0

    for qtype, spec in QUERY_TYPES.items():
        score = sum(1 for kw in spec["keywords"] if kw in q_lower)
        if score > best_score:
            best_score = score
            best_type = qtype

    spec = QUERY_TYPES[best_type]
    return QueryClassification(
        query_type=best_type,
        strategy=spec["strategy"],
        initial_tools=spec["initial_tools"],
        budget_level=spec["budget_level"],
        suggested_token_budget=spec["suggested_token_budget"],
        tool_set=spec["tools"],
    )


# ---------------------------------------------------------------------------
# LLM-based classification (optional, more accurate)
# ---------------------------------------------------------------------------

_CLASSIFY_PROMPT = """\
Classify this code question into exactly ONE category. Reply with ONLY the JSON object, no other text.

Categories:
- entry_point_discovery: finding where a feature/endpoint is defined
- business_flow_tracing: understanding a process/workflow/journey end-to-end
- root_cause_analysis: debugging errors, bugs, crashes
- impact_analysis: assessing what breaks if code changes
- architecture_question: understanding overall structure/design/modules
- config_analysis: understanding configuration/settings usage
- data_lineage: tracing how data flows through the system
- code_review: reviewing code changes in a PR or diff (e.g. "review PR master...feature/xxx")
- recent_changes: understanding recent commits, diffs, change history, who changed what

Question: {question}

Reply format: {{"query_type": "<category>"}}"""


async def classify_query_with_llm(
    question: str,
    provider: AIProvider,
) -> QueryClassification:
    """Classify using a lightweight LLM call. Falls back to keyword matching."""
    import asyncio

    try:
        prompt = _CLASSIFY_PROMPT.format(question=question[:500])
        response = await asyncio.to_thread(
            provider.chat_with_tools,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            tools=[],
            max_tokens=100,
            system="You are a query classifier. Reply with JSON only.",
        )
        text = (response.text or "").strip()
        # Parse JSON from response
        if "{" in text:
            json_str = text[text.index("{"):text.rindex("}") + 1]
            data = json.loads(json_str)
            query_type = data.get("query_type", "")
            if query_type in QUERY_TYPES:
                spec = QUERY_TYPES[query_type]
                logger.info("LLM classified query as: %s", query_type)
                # Also extract diff spec if it's a code review
                diff_ref = _detect_pr_pattern(question) if query_type == "code_review" else None
                return QueryClassification(
                    query_type=query_type,
                    strategy=spec["strategy"],
                    initial_tools=spec["initial_tools"],
                    budget_level=spec["budget_level"],
                    suggested_token_budget=spec["suggested_token_budget"],
                    tool_set=spec["tools"],
                    diff_spec=diff_ref,
                )
    except Exception as exc:
        logger.warning("LLM classification failed, falling back to keywords: %s", exc)

    return classify_query(question)
