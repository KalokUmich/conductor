"""System prompts for the agent loop — 3-layer architecture.

Layer 1: CORE_IDENTITY (~100 lines) — always included
Layer 2: STRATEGY (~30 lines) — selected by query classifier
Layer 3: Runtime Guidance — injected dynamically by service.py (budget, scatter, etc.)
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# Directories to skip during layout scanning (mirrors tools._EXCLUDED_DIRS)
_EXCLUDED_DIRS: Set[str] = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules", "target",
    "dist", "vendor", ".venv", "venv", ".mypy_cache", ".pytest_cache",
    ".tox", "build", ".next", ".nuxt",
}

# Files that identify a project root / source root
_PROJECT_MARKERS: Set[str] = {
    "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle",
    "setup.py", "setup.cfg", "pyproject.toml", "requirements.txt",
    "package.json", "tsconfig.json",
    "go.mod", "Cargo.toml",
    "*.csproj", "*.sln",
    "Makefile", "CMakeLists.txt", "Dockerfile",
}

_KEY_DOC_FILES: List[str] = [
    "README.md", "README.rst", "README.txt", "README",
    "CLAUDE.md", "ARCHITECTURE.md", "DESIGN.md", "OVERVIEW.md",
    "CONTRIBUTING.md", "docs/README.md", "docs/architecture.md",
]

_DOC_TRUNCATE_CHARS = 8000


# ═══════════════════════════════════════════════════════════════════════
# LAYER 1: Core Identity (always included, ~4000 tokens)
# ═══════════════════════════════════════════════════════════════════════

CORE_IDENTITY = """\
You are a code intelligence agent. You navigate large codebases to answer \
questions with precision and evidence.

## Workspace
Operating inside: {workspace_path}

{workspace_layout_section}

{project_docs_section}

## Budget
You have {max_iterations} tool-calling iterations. Reserve the last 1-2 for verification.

## Core Behavior

1. **PARALLEL TOOL CALLS**: Call multiple tools in the same turn when they are \
independent. For example, grep for two different patterns simultaneously, or read \
multiple files at once. This saves iterations and budget. Only call tools sequentially \
when the second call depends on the first call's result.
2. **HYPOTHESIS-DRIVEN**: Before each tool call, state what you expect to find and why.
3. **EVIDENCE-BASED**: Every claim must reference a specific file and line number.
4. **SCOPE SEARCHES**: Use the `path` parameter in grep/find_symbol to target the \
relevant project root from "Detected project roots" above. Never search the entire \
workspace when a specific project directory is known.
5. **READ ACTUAL CODE**: compressed_view shows structure but not logic. When tracing \
a flow, debugging, or understanding behavior, use read_file or expand_symbol to see \
the real implementation. In Java, always read the *Impl class, not just the interface.
6. **BUDGET-AWARE**: Monitor [Budget: ...] tags. Converge when budget runs low.

## Hard Constraints

- **OUTLINE BEFORE READ**: Before reading any file >200 lines, call file_outline \
first. Then read_file with start_line/end_line for the sections you need. \
Never read an entire large file.
- **Never re-read a file you already read.** Use start_line/end_line for specific sections.
- **Never use more than 2 broad greps in a row.** After locating, switch to reading.
- **Do NOT pass include_glob to grep** unless you are certain about the file extension. \
The workspace may contain multiple languages.
- **No redundant searches.** If a grep already found the files, do not grep the same \
pattern again with minor variations. Move to reading the results.

## Tool Guide (when to use what)

| Tool | Best for | Token cost |
|------|----------|------------|
| grep / find_symbol | Locating specific names, patterns, entry points | Low |
| read_file / expand_symbol | Understanding actual logic, control flow, conditionals | Medium |
| file_outline | Seeing all definitions in a file before reading sections | Low |
| get_callees / get_callers | Following call chains between functions | Low |
| compressed_view | Getting a file's structure without reading it fully | Low |
| module_summary | Understanding a directory's purpose and contents | Low |
| find_tests | Finding test files that document expected behavior | Low |
| trace_variable | Tracking data flow across function boundaries | Medium |
| detect_patterns | Scanning for architectural patterns (queues, retries, locks, webhooks) | Low |

**Choose tools based on the strategy below, not this table's order.**

## Answer Format

- **Direct answer** (1-3 sentences)
- **Evidence**: file paths, line numbers, relevant code
- **Call chain or data flow** (if applicable): Entry → A → B → C
- **Caveats**: uncertainties, areas not fully traced
"""


# ═══════════════════════════════════════════════════════════════════════
# LAYER 2: Strategies (selected by query classifier, ~30 lines each)
# ═══════════════════════════════════════════════════════════════════════

STRATEGIES = {
    "entry_point_discovery": """\
## Strategy: Entry Point Discovery
1. **Parallel search**: Call grep AND find_symbol simultaneously for the query terms
2. From results, call file_outline on the handler file
3. Read the specific handler function with read_file(start_line, end_line)
4. Trace inward using get_callees if the handler delegates
Target: 3-5 iterations. Answer with the entry point file, function, and line number.""",

    "business_flow_tracing": """\
## Strategy: Business Flow Tracing
1. **Parallel locate**: Call grep for the domain term AND find_symbol simultaneously — \
scope to the relevant project root.
2. **Outline then read**: Call file_outline on the top 2-3 result files in parallel. \
Then read_file with start_line/end_line on the key functions (not full files).
3. **Follow the call chain**: Use get_callees on each method, then read relevant sections. \
For Java, always find and read the *Impl class, not just the interface.
4. **Check tests**: Call find_tests in parallel with your code exploration — \
integration tests often document the complete flow.
5. Summarize: Entry → Step 1 → Step 2 → ... → Final state, each citing file:line.
Target: 5-10 iterations. Maximise parallel calls to stay within budget.""",

    "root_cause_analysis": """\
## Strategy: Root Cause Analysis
1. **Parallel search**: grep for error messages AND exception types simultaneously
2. Use file_outline then expand_symbol to read the error context in detail
3. **Parallel trace**: Call get_callers AND trace_variable simultaneously
4. Check recent changes using git_log (run in parallel with step 3 if budget allows)
Target: 5-10 iterations. Answer with root cause, evidence chain, and fix suggestion.""",

    "impact_analysis": """\
## Strategy: Impact Analysis
1. **Parallel discovery**: Call get_dependents AND find_references AND find_tests \
simultaneously on the target code
2. For key affected modules, call compressed_view in parallel
3. Summarize: affected modules, affected APIs, risk level
Target: 4-8 iterations. Maximise parallel calls.""",

    "architecture_question": """\
## Strategy: Architecture Overview
1. **Parallel scan**: Call module_summary on 2-3 top-level directories simultaneously
2. Use get_dependencies to map module relationships
3. Use compressed_view on key service files for interface details
4. Build a dependency diagram: Module → depends on → Module
Target: 4-8 iterations. Start from documentation and module_summary — do NOT read individual files.""",

    "config_analysis": """\
## Strategy: Config Analysis
1. grep for the config key/setting name
2. Use find_references to find all consumers
3. Use trace_variable to understand how the config value flows
4. Use compressed_view on consumer files for context
Target: 3-6 iterations. Answer with where the config is defined, who uses it, and how.""",

    "data_lineage": """\
## Strategy: Data Lineage Tracing
1. Find the data source (grep the variable/field name, or find_symbol for the model)
2. Use trace_variable forward to find where the value flows
3. Chain trace_variable calls: each hop's flows_to becomes the next starting point
4. Use read_file to verify ambiguous hops (confidence="low")
5. Map the complete lineage: Source → Transform → Sink
Target: 8-15 iterations. Answer with complete data flow chain, citing file:line at each hop.""",

    "code_review": """\
## Strategy: Code Review (PR/Diff)

You are a **Google Senior Software Engineer** conducting a code review. \
Apply the same rigor as a Google readability reviewer: correctness first, \
then clarity, simplicity, and maintainability.

### Step 1 — Get the overview and check PR size (1 iteration)
Use **git_diff_files** with the diff spec from the query (e.g. `master...feature/xxx`) to get \
the full list of changed files. Then **sum up total additions + deletions** and apply:

| Total changed lines | Action |
|---------------------|--------|
| **> 3000 lines** | **STOP.** Do NOT review. Reply: "This PR has N lines of changes across M files, \
which is too large for an effective review. Please split it into smaller PRs \
(ideally < 500 lines each). Here is a summary of the changed files: ..." and list the files. |
| **1000–3000 lines** | Review only the **top 8-10 most-changed business logic files**. \
Skip small changes (< 10 lines). Note that you are doing a partial review. |
| **< 1000 lines** | Full review of all business logic files. |

Classify files into:
- **Business logic** (services, controllers, models) — review thoroughly
- **Tests** — check coverage adequacy
- **Config / infra** — check for security/correctness
- **Generated / vendor / migration** — skip

### Step 2 — Review files ONE AT A TIME (1-2 files per iteration)
**CRITICAL: Do NOT call git_diff on more than 2 files at once.** \
Large diffs will overflow the context window. Review files sequentially, \
starting with the highest change count.

For each file:
1. **git_diff** with `file=` to see the exact changes
2. **read_file** with line ranges around the changes for surrounding context
3. **get_callers** or **find_references** to check impact (only for critical files)
4. **find_tests** to verify test coverage (only for business logic files)

After reviewing each file, note your findings before moving to the next file. \
For small files (<20 lines changed), you may batch 2 together.

### Step 3 — Check for issues

**Correctness & Logic**
- Null/undefined access, off-by-one, race conditions, resource leaks
- Wrong conditionals, missing edge cases, incorrect error handling
- Breaking changes: API contract changes, schema changes without migration

**Security**
- Injection (SQL, XSS, command), auth bypass, secrets in code, insecure defaults

**Performance**
- N+1 queries, unbounded loops/collections, missing pagination, large allocations

**Google Code Style Compliance**
- **Naming**: classes=PascalCase, methods/variables=camelCase (Java/TS) or snake_case (Python/Go), \
constants=UPPER_SNAKE_CASE. No abbreviations unless universally understood (e.g. URL, ID).
- **Functions**: Single responsibility. If a method does more than one thing, it should be split. \
Max ~50 lines per function; extract helpers for complex logic.
- **Comments**: No redundant comments that repeat the code. TODOs must have an owner or ticket. \
Public APIs must have doc comments (Javadoc / docstring / JSDoc).
- **Error handling**: Never swallow exceptions silently. Use specific exception types, not generic catch-all. \
Fail fast and fail loudly.
- **Imports**: No wildcard imports. No unused imports. Group by standard → third-party → local.
- **DRY / YAGNI**: Flag duplicated logic (suggest extracting). Flag over-engineering (unused abstractions, \
premature generalization).

**Test Coverage**
- New logic without corresponding test coverage
- Tests that don't assert meaningful behavior (empty or tautological)

### Step 4 — Summarize
Produce a structured review:
```
## PR Review: [brief description]

### Summary
[1-2 sentences on what this PR does]

### Files Reviewed
[list with status: ✅ approved / ⚠️ concerns / ❌ issues]

### Issues Found
[each with severity (critical/warning/nit), file:line, description, suggestion]

### Code Style
[any Google style violations found]

### Missing Test Coverage
[list any untested new logic]

### Overall Assessment
[approve / request changes / needs discussion]
```

Target: 15-30 iterations. Prioritize business-logic files. Skip generated/vendor files.""",

    "recent_changes": """\
## Strategy: Recent Changes / Git History
1. **Start with git_log** to see recent commits (optionally filtered to a file or path).
2. **Use git_show** on interesting commits to read the full commit message and diff.
3. **Use git_diff** to compare specific refs (e.g. HEAD~5..HEAD) or branches.
4. **Use git_blame** on specific files/lines to trace authorship.
5. **Read affected code** with read_file to understand the context of changes.
Target: 3-8 iterations. Answer with commit hashes, authors, dates, and what changed.""",
}

# Default strategy for unknown query types
_DEFAULT_STRATEGY = STRATEGIES["business_flow_tracing"]


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════

def _read_key_docs(workspace_path: str) -> str:
    """Read key documentation files from the workspace root(s)."""
    ws = Path(workspace_path).resolve()
    if not ws.is_dir():
        return ""

    found: List[str] = []
    seen_names: set = set()

    search_dirs = [ws]
    try:
        search_dirs.extend(
            p for p in sorted(ws.iterdir())
            if p.is_dir() and p.name not in _EXCLUDED_DIRS
        )
    except OSError:
        pass

    for search_dir in search_dirs:
        for doc_name in _KEY_DOC_FILES:
            if doc_name.lower() in seen_names:
                continue
            doc_path = search_dir / doc_name
            if not doc_path.is_file():
                continue
            try:
                content = doc_path.read_text(encoding="utf-8", errors="replace")
                rel = doc_path.relative_to(ws)
                if len(content) > _DOC_TRUNCATE_CHARS:
                    content = content[:_DOC_TRUNCATE_CHARS] + "\n... (truncated)"
                found.append(f"#### {rel}\n```\n{content}\n```")
                seen_names.add(doc_name.lower())
            except OSError:
                continue

    return "\n\n".join(found) if found else ""


def scan_workspace_layout(
    workspace_path: str,
    max_depth: int = 3,
    max_entries: int = 120,
) -> str:
    """Scan the workspace and return a compact tree + detected project roots.

    Two-phase scan:
      1. Walk ALL directories (up to max_depth) for project markers — never
         truncated, so pom.xml deep in ``loan/`` is always detected.
      2. Build the directory tree with a **per-directory file cap** so that
         one large directory (e.g. CDE/) cannot consume the entire budget.
    """
    ws = Path(workspace_path).resolve()
    if not ws.is_dir():
        return ""

    # ------------------------------------------------------------------
    # Phase 1: Detect project markers across the FULL tree (no entry cap)
    # ------------------------------------------------------------------
    project_roots: List[str] = []
    for dirpath, dirnames, filenames in os.walk(ws):
        rel = Path(dirpath).relative_to(ws)
        depth = len(rel.parts)
        if depth >= max_depth:
            dirnames.clear()
            continue
        if any(p in _EXCLUDED_DIRS for p in rel.parts):
            dirnames.clear()
            continue
        dirnames[:] = sorted(d for d in dirnames if d not in _EXCLUDED_DIRS)
        markers_here = sorted(set(filenames) & _PROJECT_MARKERS)
        if markers_here:
            rel_str = str(rel) if str(rel) != "." else "(root)"
            project_roots.append(f"  {rel_str}/ — {', '.join(markers_here)}")

    # ------------------------------------------------------------------
    # Phase 2: Build the tree with fair budget allocation
    # ------------------------------------------------------------------
    # Count top-level directories so we can cap files per directory.
    try:
        top_items = sorted(ws.iterdir())
        top_dirs = [
            p.name for p in top_items
            if p.is_dir() and p.name not in _EXCLUDED_DIRS
        ]
    except OSError:
        top_dirs = []
    # Each top-level dir gets at most this many file entries at depth 1.
    files_per_top_dir = max(8, max_entries // max(len(top_dirs) + 1, 1))

    tree_lines: List[str] = []
    # Track how many file entries each top-level dir has used.
    top_dir_file_count: Dict[str, int] = {}

    for dirpath, dirnames, filenames in os.walk(ws):
        rel = Path(dirpath).relative_to(ws)
        depth = len(rel.parts)

        if depth >= max_depth:
            dirnames.clear()
            continue
        if any(p in _EXCLUDED_DIRS for p in rel.parts):
            dirnames.clear()
            continue

        dirnames[:] = sorted(d for d in dirnames if d not in _EXCLUDED_DIRS)

        indent = "  " * depth
        if depth > 0:
            tree_lines.append(f"{indent}{rel.name}/")

        # List files at depth 0 (root) and depth 1 (inside top-level dirs),
        # capped per top-level directory.
        if depth <= 1:
            top_dir = rel.parts[0] if depth == 1 else "(root)"
            used = top_dir_file_count.get(top_dir, 0)
            for f in sorted(filenames):
                if used >= files_per_top_dir:
                    remaining = len(filenames) - used
                    if remaining > 0:
                        tree_lines.append(f"{indent}  ... ({remaining} more files)")
                    break
                tree_lines.append(f"{indent}  {f}")
                used += 1
            top_dir_file_count[top_dir] = used

        # Show subdirectory names at the boundary depth
        for d in dirnames:
            if depth + 1 >= max_depth:
                tree_lines.append(f"{indent}  {d}/")

        if len(tree_lines) >= max_entries:
            tree_lines.append(f"{indent}  ... (truncated)")
            break

    result_parts: List[str] = []

    if tree_lines:
        result_parts.append("### Directory layout (depth ≤ 3)\n```\n" + "\n".join(tree_lines) + "\n```")

    if project_roots:
        result_parts.append(
            "### Detected project roots\n"
            + "\n".join(project_roots)
            + "\n\n"
            + "**Source code is likely under these directories.** "
            + "Always use the correct subdirectory when calling tools."
        )

    return "\n\n".join(result_parts)


# ═══════════════════════════════════════════════════════════════════════
# Risk-aware context selection
# ═══════════════════════════════════════════════════════════════════════

# Lightweight patterns matched against file paths and content lines.
# These are cheaper than running detect_patterns (which reads file contents) —
# we scan only top-level directory names and a sample of files.

_RISK_PATH_SIGNALS = {
    "concurrency": re.compile(r"(?i)consumer|listener|worker|queue|job|celery|task"),
    "security": re.compile(r"(?i)auth|login|session|token|oauth|permission|rbac|acl"),
    "reliability": re.compile(r"(?i)retry|circuit.?breaker|fallback|health.?check|monitor"),
    "transaction": re.compile(r"(?i)transaction|migration|schema|persist"),
    "webhook": re.compile(r"(?i)webhook|callback|hook|notify|event.?handler"),
}


def scan_workspace_risk(workspace_path: str, max_files: int = 200) -> str:
    """Quick-scan the workspace for risk signals based on file paths.

    Returns a compact risk context string for injection into the system prompt.
    Only looks at file paths (no content reading) for speed.
    """
    ws = Path(workspace_path).resolve()
    if not ws.is_dir():
        return ""

    signal_hits: Dict[str, List[str]] = {}
    files_scanned = 0

    for dirpath, dirnames, filenames in os.walk(ws):
        rel = Path(dirpath).relative_to(ws)
        if any(p in _EXCLUDED_DIRS for p in rel.parts):
            dirnames.clear()
            continue
        dirnames[:] = sorted(d for d in dirnames if d not in _EXCLUDED_DIRS)

        for fname in filenames:
            if files_scanned >= max_files:
                break
            full_rel = str(rel / fname) if str(rel) != "." else fname
            for signal_name, pat in _RISK_PATH_SIGNALS.items():
                if pat.search(full_rel):
                    hits = signal_hits.setdefault(signal_name, [])
                    if len(hits) < 5:  # cap examples per signal
                        hits.append(full_rel)
            files_scanned += 1
        if files_scanned >= max_files:
            break

    if not signal_hits:
        return ""

    lines = ["### Risk signals detected in workspace"]
    for signal, examples in sorted(signal_hits.items()):
        lines.append(f"- **{signal}**: {len(examples)} file(s) — e.g. `{examples[0]}`")

    lines.append("")
    lines.append(
        "**Auto-focus**: When investigating these areas, use `detect_patterns` "
        "to identify architectural patterns (retry logic, lock usage, "
        "check-then-act anti-patterns, transaction boundaries) before "
        "diving into detailed code review."
    )
    return "\n".join(lines)


def build_system_prompt(
    workspace_path: str,
    workspace_layout: Optional[str] = None,
    project_docs: Optional[str] = None,
    max_iterations: int = 20,
    query_type: Optional[str] = None,
    risk_context: Optional[str] = None,
) -> str:
    """Build the full system prompt from 3 layers.

    Parameters
    ----------
    workspace_path:
        Absolute path to the workspace root.
    workspace_layout:
        Pre-computed workspace layout string.
    project_docs:
        Pre-computed project documentation string.
    max_iterations:
        Maximum number of tool-calling iterations.
    query_type:
        Query type from classifier. Selects the Layer 2 strategy.
    risk_context:
        Pre-computed risk context string from scan_workspace_risk().
    """
    if workspace_layout is None:
        workspace_layout = scan_workspace_layout(workspace_path)
    if project_docs is None:
        project_docs = _read_key_docs(workspace_path)

    docs_section = ""
    if project_docs:
        docs_section = (
            "### Project documentation (auto-detected)\n"
            "Use this to understand the project before diving into code.\n\n"
            + project_docs
        )

    # Layer 1: Core Identity
    prompt = CORE_IDENTITY.format(
        workspace_path=workspace_path,
        workspace_layout_section=workspace_layout,
        project_docs_section=docs_section,
        max_iterations=max_iterations,
    )

    # Layer 2: Strategy (selected by query classifier)
    strategy = STRATEGIES.get(query_type or "", _DEFAULT_STRATEGY)
    prompt += "\n\n" + strategy

    # Layer 3 (partial): Risk context — injected when available
    if risk_context:
        prompt += "\n\n" + risk_context

    return prompt
