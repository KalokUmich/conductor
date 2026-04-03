"""Per-tool output truncation policies.

Replaces the uniform 30KB hard cutoff with differentiated strategies
that consider the nature of each tool's output.

Design goals:
  * grep / find_references: limit result count, not chars (each match is small)
  * read_file: truncate at function/class boundaries when possible
  * list_files: limit depth + entry count
  * Default: character-based truncation
  * Budget-adaptive: tighter limits when remaining tokens < 100K

Reference: CONDUCATOR_IMPLEMENTATION_SPEC.md — Tool Output Policy
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class OutputPolicy:
    """Truncation policy for a single tool."""

    max_results: Optional[int] = None  # For list-type results
    max_chars: int = 30_000  # Character limit for serialized output
    truncate_unit: str = "chars"  # "chars", "results", "lines"
    budget_adaptive: bool = True  # Shrink limits when budget is low


# Per-tool policies
_TOOL_POLICIES: dict[str, OutputPolicy] = {
    # Search tools — limit result count, not chars
    "grep": OutputPolicy(max_results=40, max_chars=40_000, truncate_unit="results"),
    "find_references": OutputPolicy(max_results=30, max_chars=30_000, truncate_unit="results"),
    "find_symbol": OutputPolicy(max_results=20, max_chars=20_000, truncate_unit="results"),
    "find_tests": OutputPolicy(max_results=20, max_chars=20_000, truncate_unit="results"),
    "get_callers": OutputPolicy(max_results=20, max_chars=20_000, truncate_unit="results"),
    "get_callees": OutputPolicy(max_results=30, max_chars=20_000, truncate_unit="results"),
    # File reading — generous char limit (users often need full context)
    "read_file": OutputPolicy(max_chars=50_000, truncate_unit="lines"),
    # Outline / structure tools — moderate limits
    "file_outline": OutputPolicy(max_results=100, max_chars=20_000, truncate_unit="results"),
    "test_outline": OutputPolicy(max_results=50, max_chars=20_000, truncate_unit="results"),
    # Directory listing & file matching — limit entries
    "list_files": OutputPolicy(max_results=100, max_chars=15_000, truncate_unit="results"),
    "glob": OutputPolicy(max_results=100, max_chars=15_000, truncate_unit="results"),
    # Dependency tools — moderate
    "get_dependencies": OutputPolicy(max_results=50, max_chars=15_000, truncate_unit="results"),
    "get_dependents": OutputPolicy(max_results=50, max_chars=15_000, truncate_unit="results"),
    # Git tools — generous (diffs can be large)
    "git_diff_files": OutputPolicy(max_results=100, max_chars=20_000, truncate_unit="results"),
    "git_diff": OutputPolicy(max_chars=40_000, truncate_unit="chars"),
    "git_log": OutputPolicy(max_results=30, max_chars=20_000, truncate_unit="results"),
    "git_blame": OutputPolicy(max_results=100, max_chars=30_000, truncate_unit="results"),
    "git_show": OutputPolicy(max_chars=40_000, truncate_unit="chars"),
    # AST / trace tools
    "ast_search": OutputPolicy(max_results=20, max_chars=30_000, truncate_unit="results"),
    "trace_variable": OutputPolicy(max_chars=20_000, truncate_unit="chars"),
    # Compressed / summary tools — moderate (they're already compact)
    "compressed_view": OutputPolicy(max_chars=30_000, truncate_unit="chars"),
    "module_summary": OutputPolicy(max_chars=20_000, truncate_unit="chars"),
    "expand_symbol": OutputPolicy(max_chars=50_000, truncate_unit="chars"),
    # Pattern detection — limit result count
    "detect_patterns": OutputPolicy(max_results=50, max_chars=30_000, truncate_unit="results"),
}

# Default policy for unknown tools
_DEFAULT_POLICY = OutputPolicy(max_chars=30_000, truncate_unit="chars")


def get_policy(tool_name: str) -> OutputPolicy:
    """Return the output policy for a tool."""
    return _TOOL_POLICIES.get(tool_name, _DEFAULT_POLICY)


def apply_policy(
    tool_name: str,
    data: Any,
    remaining_input_tokens: Optional[int] = None,
) -> str:
    """Serialize tool output data with per-tool truncation.

    Args:
        tool_name: Name of the tool that produced the data.
        data: The tool result data (dict, list, or scalar).
        remaining_input_tokens: If provided and < 100K, shrink limits by 50%.

    Returns:
        JSON string, truncated according to the tool's policy.
    """
    policy = get_policy(tool_name)

    # Budget-adaptive: shrink limits when running low on tokens
    max_results = policy.max_results
    max_chars = policy.max_chars
    if policy.budget_adaptive and remaining_input_tokens is not None and remaining_input_tokens < 100_000:
        if max_results is not None:
            max_results = max(5, max_results // 2)
        max_chars = max(5_000, max_chars // 2)

    # Truncate by result count for list-type data
    if max_results is not None and isinstance(data, list) and len(data) > max_results:
        original_count = len(data)
        data = data[:max_results]
        text = json.dumps(data, default=str)
        text += f"\n... ({original_count - max_results} more results truncated)"
    else:
        text = json.dumps(data, default=str)

    # Truncate by character limit
    if len(text) > max_chars:
        if policy.truncate_unit == "lines":
            # Try to cut at a line boundary
            truncated = text[:max_chars]
            last_nl = truncated.rfind("\n")
            if last_nl > max_chars * 0.8:
                truncated = truncated[:last_nl]
            text = truncated + "\n... (truncated)"
        else:
            text = text[:max_chars] + "\n... (truncated)"

    return text
