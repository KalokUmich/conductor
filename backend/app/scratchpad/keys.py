"""Canonical key builders for the Fact Vault.

A cache key encodes the identity of a tool call so two callers that will
produce the same result hash to the same key. Consistency of the key scheme
is critical — any deviation (different path separator, different quoting,
different glob ordering) produces spurious cache misses.

Schema version is prefixed (``v1:``) so a future change in tool semantics
can invalidate old entries without corrupting correctness — bump to
``v2:`` and old facts become unreachable.

Key shape per tool:

    v1:grep:<pattern>:<path>:<glob>:<type>
    v1:read_file:<abs_path>:<start>:<end>
    v1:find_symbol:<symbol>:<path_prefix>
    v1:find_references:<symbol>:<path_prefix>
    v1:ast_search:<sha16>:<abs_path>
    v1:get_dependencies:<abs_path>
    v1:get_dependents:<abs_path>
    v1:file_outline:<abs_path>
    v1:test_outline:<abs_path>
    v1:ensure_graph:<abs_workspace>
    v1:symbol_index:<abs_workspace>:<git_head>
    v1:git_diff:<spec>:<abs_path>
    v1:git_diff_files:<spec>

The line-range tools (``read_file``, ``git_blame``) also populate the
``range_start`` / ``range_end`` columns in SQLite so the FactStore can do
range-intersection lookup: a request for line 101-130 hits a cached 100-150
entry. That's enforced by ``FactStore.range_lookup()`` — not in the key.

Canonicalisation rules (apply before hashing/storing):
  * paths: absolute, os.path.realpath-resolved (symlinks collapsed)
  * patterns: strip whitespace, normalise escapes
  * globs / type sets: sorted alphabetically before join
  * lowercase tool names

Callers should treat the returned string as opaque — parsing individual
segments out of it is brittle.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any, Dict, Optional, Sequence

SCHEMA_VERSION = "v1"

# Tools whose output varies with line range — the range is part of the key
# AND gets indexed in SQLite for range-intersection queries.
RANGE_TOOLS = frozenset({"read_file", "git_blame"})

# Tools whose results are DETERMINISTIC and cacheable. Anything touching
# external state (web), time (git_log without --before), or doing mutation
# (file_edit, file_write, run_test) must NOT be cached.
CACHEABLE_TOOLS = frozenset(
    {
        "grep",
        "read_file",
        "find_symbol",
        "find_references",
        "file_outline",
        "test_outline",
        "ast_search",
        "get_dependencies",
        "get_dependents",
        "get_callers",
        "get_callees",
        "trace_variable",
        "compressed_view",
        "module_summary",
        "expand_symbol",
        "detect_patterns",
        "list_files",
        "glob",
        "extract_docstrings",
        "list_endpoints",
        "git_diff",
        "git_diff_files",
        "git_show",
        "git_blame",
        "find_tests",
        "db_schema",
    }
)


def _abs(path: Optional[str]) -> str:
    """Canonicalise a path. Returns empty string for None so the key still
    builds cleanly — callers that pass None for ``path`` want the cache
    keyed without a path component."""
    if not path:
        return ""
    try:
        return os.path.realpath(os.path.abspath(path))
    except OSError:
        # Broken symlink — realpath can raise on some FS. Fall back to
        # abspath so the key is still deterministic.
        return os.path.abspath(path)


def _norm_pattern(s: Optional[str]) -> str:
    if not s:
        return ""
    return s.strip()


def _norm_set(values: Optional[Sequence[str]]) -> str:
    if not values:
        return ""
    return ",".join(sorted(set(values)))


def _hash16(s: str) -> str:
    """16-char SHA256 prefix — enough for cache-key uniqueness at our scale."""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def build_key(tool: str, params: Dict[str, Any]) -> Optional[str]:
    """Build a canonical cache key for ``(tool, params)``.

    Returns ``None`` for tools that are not cacheable. Caller then
    short-circuits to run the tool directly without consulting the vault.

    Params that don't affect the tool's output (e.g. ``limit``, ``offset``,
    budget hints) are intentionally ignored here — including them would
    turn semantically-equivalent calls into distinct keys and blow cache
    hit rate. When in doubt, prefer a narrower key (fewer fields) and let
    callers explicitly pass the params that really matter.
    """
    if tool not in CACHEABLE_TOOLS:
        return None

    t = tool.lower()
    v = SCHEMA_VERSION

    if t == "grep":
        return ":".join(
            [
                v,
                t,
                _norm_pattern(params.get("pattern")),
                _abs(params.get("path")),
                _norm_set(params.get("glob") or params.get("globs")),
                _norm_pattern(params.get("file_type") or params.get("type")),
                # context_lines, case_insensitive, multiline are query refiners
                # — different values ARE different outputs. Include them.
                str(params.get("context_lines", "")),
                "ci" if params.get("case_insensitive") else "",
                "ml" if params.get("multiline") else "",
            ]
        )

    if t == "read_file":
        start = params.get("start_line")
        end = params.get("end_line")
        # Always stringify — "" for None so the key is well-formed
        return ":".join(
            [
                v,
                t,
                _abs(params.get("path") or params.get("file_path")),
                str(start) if start is not None else "",
                str(end) if end is not None else "",
            ]
        )

    if t == "git_blame":
        start = params.get("start_line")
        end = params.get("end_line")
        return ":".join(
            [
                v,
                t,
                _abs(params.get("path") or params.get("file")),
                str(start) if start is not None else "",
                str(end) if end is not None else "",
                str(params.get("revision", "")),
            ]
        )

    if t in ("find_symbol", "find_references"):
        return ":".join(
            [
                v,
                t,
                _norm_pattern(params.get("symbol") or params.get("name")),
                _abs(params.get("path") or params.get("path_prefix")),
            ]
        )

    if t == "ast_search":
        pat_hash = _hash16(_norm_pattern(params.get("pattern")))
        return ":".join([v, t, pat_hash, _abs(params.get("path"))])

    if t in ("file_outline", "test_outline", "get_dependencies", "get_dependents", "extract_docstrings"):
        return ":".join([v, t, _abs(params.get("path") or params.get("file_path"))])

    if t in ("get_callers", "get_callees"):
        return ":".join(
            [
                v,
                t,
                _norm_pattern(params.get("symbol") or params.get("name")),
                _abs(params.get("path") or params.get("file_path")),
            ]
        )

    if t in ("trace_variable", "compressed_view", "module_summary", "expand_symbol"):
        return ":".join(
            [
                v,
                t,
                _norm_pattern(params.get("symbol") or params.get("name") or ""),
                _abs(params.get("path") or params.get("file_path")),
            ]
        )

    if t == "detect_patterns":
        return ":".join([v, t, _abs(params.get("path") or params.get("file_path")), _norm_set(params.get("patterns"))])

    if t in ("list_files", "glob"):
        return ":".join(
            [
                v,
                t,
                _abs(params.get("path") or "."),
                _norm_pattern(params.get("pattern")),
            ]
        )

    if t == "list_endpoints":
        return ":".join([v, t, _abs(params.get("path") or ".")])

    if t == "find_tests":
        return ":".join(
            [
                v,
                t,
                _norm_pattern(params.get("symbol") or ""),
                _abs(params.get("path") or ""),
            ]
        )

    if t == "db_schema":
        return ":".join([v, t, _abs(params.get("path") or ".")])

    if t == "git_diff":
        return ":".join(
            [
                v,
                t,
                _norm_pattern(params.get("spec") or params.get("diff_spec") or ""),
                _abs(params.get("path") or params.get("file_path") or ""),
            ]
        )

    if t == "git_diff_files":
        return ":".join([v, t, _norm_pattern(params.get("spec") or params.get("diff_spec") or "")])

    if t == "git_show":
        return ":".join(
            [
                v,
                t,
                _norm_pattern(params.get("revision") or params.get("commit") or ""),
                _abs(params.get("path") or ""),
            ]
        )

    # Defensive fallback for tools listed in CACHEABLE_TOOLS that don't
    # match above — key by sorted JSON of params. Works but is brittle
    # (any param reorder changes the key); should add explicit branch above.
    import json

    return ":".join([v, t, _hash16(json.dumps(params, sort_keys=True, default=str))])


def extract_path(tool: str, params: Dict[str, Any]) -> Optional[str]:
    """Pull the canonical absolute path out of a tool's params, or None.

    Used by FactStore's range-intersection lookup to prefix-filter on path
    without parsing the full key.
    """
    t = tool.lower()
    if t in ("read_file", "git_blame"):
        p = params.get("path") or params.get("file_path") or params.get("file")
        return _abs(p) if p else None
    if t in (
        "file_outline",
        "test_outline",
        "get_dependencies",
        "get_dependents",
        "extract_docstrings",
        "detect_patterns",
        "find_symbol",
        "find_references",
        "get_callers",
        "get_callees",
        "trace_variable",
        "compressed_view",
        "module_summary",
        "expand_symbol",
    ):
        p = params.get("path") or params.get("file_path") or params.get("path_prefix")
        return _abs(p) if p else None
    return None


def extract_range(tool: str, params: Dict[str, Any]) -> tuple:
    """Return (start, end) ints for line-range tools, or (None, None)."""
    if tool.lower() not in RANGE_TOOLS:
        return (None, None)
    start = params.get("start_line")
    end = params.get("end_line")
    try:
        start_i = int(start) if start is not None else None
        end_i = int(end) if end is not None else None
    except (TypeError, ValueError):
        return (None, None)
    return (start_i, end_i)
