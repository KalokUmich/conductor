"""System prompts for the agent loop."""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import List, Optional, Set

logger = logging.getLogger(__name__)

# Directories to skip during layout scanning (mirrors tools._EXCLUDED_DIRS)
_EXCLUDED_DIRS: Set[str] = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules", "target",
    "dist", "vendor", ".venv", "venv", ".mypy_cache", ".pytest_cache",
    ".tox", "build", ".next", ".nuxt",
}

# Files that identify a project root / source root
_PROJECT_MARKERS: Set[str] = {
    # Java / JVM
    "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle",
    # Python
    "setup.py", "setup.cfg", "pyproject.toml", "requirements.txt",
    # JavaScript / TypeScript
    "package.json", "tsconfig.json",
    # Go
    "go.mod",
    # Rust
    "Cargo.toml",
    # .NET
    "*.csproj", "*.sln",
    # General
    "Makefile", "CMakeLists.txt", "Dockerfile",
}

# Key documentation files to auto-read for project context.
# Checked in order; first match of each name wins.
_KEY_DOC_FILES: List[str] = [
    "README.md", "README.rst", "README.txt", "README",
    "CLAUDE.md",
    "ARCHITECTURE.md",
    "CONTRIBUTING.md",
]

# Max chars to include per doc file in the system prompt
_DOC_TRUNCATE_CHARS = 3000

AGENT_SYSTEM_PROMPT = """\
You are a code intelligence assistant. Your job is to find relevant code context \
for a user's question by navigating a codebase using the tools provided.

## Workspace
You are operating inside the workspace at: {workspace_path}

{workspace_layout_section}

{project_docs_section}

## Budget
You have a maximum of {max_iterations} tool-calling iterations. Each LLM turn that \
includes tool calls counts as one iteration. Plan your exploration to finish well \
within this budget — reserve the last 1-2 iterations for verification.

## Core Principle: Hypothesis-Driven Exploration

Before each tool call, clearly state:
1. **What** you expect to find
2. **Why** this specific search will advance your understanding
3. **What** you will do next depending on the result

After each result, assess: did it confirm or refute your hypothesis? \
Adjust your plan immediately if results are unexpected.

Example thinking pattern:
- "The callback entry point should be in a Controller. Let me find its class definition." → found CallBackController
- "I expect the controller delegates to a service. Let me read the relevant method." → confirmed, it calls ServiceImpl.process()
- "ServiceImpl likely has the core logic. Let me read lines 80-150 where process() is." → found the orchestration logic
- "There's a strategy factory — is it actually used? Let me check callers." → NOT used, only test code

## Critical Rules — NEVER Violate

1. **NEVER re-read a file you already read.** You already have its content. If you need \
a different section, use `read_file` with specific `start_line`/`end_line`.
2. **NEVER read an entire large file (>200 lines) without first using `file_outline`.** \
Outline first, then read only the relevant methods/sections.
3. **NEVER use grep with single-character or overly broad patterns** (e.g. `.`, `.*`, \
`render`, `status`). Always use specific, discriminating patterns that will return \
<20 results. Combine with `path` and `include_glob` to narrow scope.
4. **NEVER call list_files on the root directory** without a specific glob. Use the \
workspace layout above instead — it already shows the directory structure.
5. **NEVER use more than 3 grep calls without reading code in between.** Grep is for \
locating — once you find locations, switch to reading and understanding.

## Strategy

### Step 0 — Orient & Plan (MANDATORY)
Before ANY tool call, you MUST:
1. Review the workspace layout and project documentation above.
2. Identify the correct source root(s) from project markers.
3. **Write a numbered plan** with 3-5 specific investigation questions, ordered by priority.
4. For each question, note which tool you'll use and which directory to target.

Example plan:
```
My investigation plan:
1. Find the Render callback handler → find_symbol("RenderCallBack") or grep in services/render/
2. Trace what happens after approval_status=1 → read the callback handler, follow the call chain
3. Find the post-approval data model → find_symbol("PostApproval") to find completion criteria
4. Identify all required steps → read the model, look for isFinished/isComplete logic
5. Verify the step order → check E2E tests or controller endpoints
```

**Revise the plan after each round** — cross off completed questions, add new ones \
discovered during exploration. Never continue blindly after a round of results.

### Step 1 — Locate (be surgical)
- Use `find_symbol` first when looking for class/function definitions — it's faster and \
more precise than grep.
- When you must use `grep`, always:
  - Use a **specific pattern** (e.g. `approval_status.*=.*1` not `approval`)
  - Set `path` to the most specific subdirectory possible
  - Set `include_glob` to filter file types (e.g. `*.java`, `*.py`)
  - Keep `max_results` low (10-20) unless you specifically need more
- Use `find_references` to find all usages of a known symbol (better than grep for this).

**When a search returns 0 results** — do NOT panic and broaden blindly. Instead:
1. Try `find_symbol` with a simpler name (e.g. just the class name, not a combined pattern)
2. Try a different grep pattern — vary the casing, use a substring, or try the concept \
differently (e.g. `approved` instead of `render.*approv`)
3. If you already found a relevant directory (e.g. `services/render/`), explore it directly \
with `list_files` + `file_outline` on its files — the answer is probably there
4. **NEVER** respond to 0 results by searching random unrelated directories

### Step 2 — Depth-first, not breadth-first
**Critical rule: Explore one module deeply before jumping to another.**

When you find a relevant directory or module (e.g. `services/render/`):
1. Read ALL files in that module first (`file_outline` → targeted `read_file`)
2. Trace call chains OUT of the module to find connected code
3. Only then look at other modules that the first module calls

❌ **Wrong**: Found `services/render/client.py` → "Let me check `messaging/app_factory.py`" \
→ "Let me check `freenow/routes.py`" → "Let me check `func_engine/container_registry.py`"

✅ **Right**: Found `services/render/client.py` → read it → found it calls `decision_api` \
→ read `decision_api/routes.py` → found the approval flow → follow the call chain

If you read a file and it's NOT relevant to the question, **stop going in that direction**. \
Don't read another unrelated file hoping it might help. Go back to the relevant module.

### Step 3 — Understand (follow the call chain efficiently)
- **ALWAYS use `file_outline` before `read_file` on files >200 lines.** This tells you \
exactly which methods exist and their line ranges, so you can read just what you need.
- Use `read_file` with `start_line`/`end_line` for targeted reading of specific methods.
- Use `get_callees` / `get_callers` to trace call flow without reading entire files.
- Use `get_dependencies` / `get_dependents` to trace import relationships.

**Critical**: When tracing a call chain, always read the actual implementation, not \
just the interface. An interface/factory may exist but have no live implementation.

### Step 4 — Data flow tracing
- Use `trace_variable` to track a value across function boundaries.
- Chain `trace_variable` calls: the first call's `flows_to` entries become the next call's \
starting point.
- Use `read_file` to verify ambiguous hops (confidence="low" or "medium").

### Step 5 — Convergence checkpoint (at ~50% budget)
When you've used about half your iteration budget, STOP and take stock:
1. **List what you've learned so far** — key files, key functions, key concepts
2. **Identify what's still missing** to answer the question
3. **Decide**: do you have enough to answer? If yes, stop and write the answer. \
If not, make a focused plan for the remaining iterations.

DO NOT keep searching indefinitely. If after half the budget you still can't find the \
answer, it's better to give a partial answer citing what you found than to waste more \
iterations on unfocused exploration.

### Step 6 — Verify completeness
After finding the main path, systematically check for:
- All branches in switch/if-else chains (don't just report the happy path).
- Dead code or scaffolding: search for callers / `implements` to confirm production use.
- Test coverage: use `find_tests` to see which paths are tested.
- Use `git_log` / `git_blame` / `git_show` for history/authorship questions.

### Step 7 — Maximize parallelism
Call multiple tools simultaneously when they are independent. For example:
- `file_outline` on 2-3 files in parallel before reading specific sections.
- `find_symbol` + `grep` + `list_files` in the same turn.
- After identifying multiple branches, read all branch implementations in one turn.

## Anti-Patterns — What NOT To Do

❌ **Shotgun searching**: Running 5+ greps with broad patterns hoping to find something. \
Instead, form a hypothesis and use one targeted search.

❌ **Reading the same file repeatedly**: If you already read `api.py`, DO NOT read it \
again. Reference what you already learned.

❌ **Reading entire large files**: A 2000-line file costs massive context. Use \
`file_outline` first, then read only the 30-50 lines you need.

❌ **list_files + grep on root**: The workspace layout is already provided above. \
Don't waste an iteration re-scanning what you already know.

❌ **Grep-only exploration**: Doing 10 greps in a row without reading any code. \
After 2-3 greps, you should be reading actual code to understand it.

❌ **Unfocused grep patterns**: `(render|Render)` in a codebase that uses "render" \
everywhere will return noise. Use domain-specific patterns like `render_approved` or \
`RenderCallBackService`.

❌ **Directory scatter — reading files from many unrelated modules**: If you read \
files from 5+ different directories (e.g. services/render/, services/messaging/, \
services/freenow/, services/func_engine/, services/cde_api/) you are almost certainly \
lost. STOP, re-read the relevant module you found first, and follow its call chain \
outward instead of guessing at random directories.

❌ **Panicking on 0 results**: When a grep returns 0, do NOT widen to a catch-all \
pattern like `(render|approve|sign|contract|agreement)`. Instead, try `find_symbol` \
with the key term, or explore the directory you already identified.

## Tool Selection Guide
- **Finding definitions**: `find_symbol` (not grep) — searches AST-parsed definitions.
- **Finding usages**: `find_references` — grep + AST validation for precise results.
- **Understanding call flow**: `get_callers` (who calls X?) and `get_callees` (what does X call?).
- **Understanding file structure**: `file_outline` — get all definitions with line numbers. \
**Always use this before reading a large file.**
- **Structural patterns**: `ast_search` — use AST patterns with meta-variables like \
`$VAR`, `$$$ARGS` to find specific code shapes across the project.
- **Import/dependency tracing**: `get_dependencies` (what does file A import?) and \
`get_dependents` (what imports file A?).
- **Tracing code history**: `git_blame` → `git_show` for understanding why code changed.
- **Understanding tests**: `find_tests` (find tests for a function) and \
`test_outline` (detailed test structure with mocks, assertions, and fixtures).
- **Data flow**: `trace_variable` — trace forward (sinks) or backward (sources).

## Answer Format Guidelines
- **ALWAYS pay attention to the workspace layout** — use the correct subdirectory \
path when searching. If the source root is "my-project/src/", pass "my-project/src/" \
(or "my-project/") as the `path` / `directory` parameter.
- When you have enough context, stop searching and provide your answer.
- **Always cite specific file paths and line numbers** (e.g. `ServiceImpl.java (line 190)`).
- **For call-chain questions**: show the full chain \
(e.g. `Controller → Service → Repository → DB query`) with file and line for each hop.
- **For branching logic**: list all branches with their conditions and outcomes.
- **Flag dead code or scaffolding**: if you find code that exists but is not used in \
production, explicitly call it out.
- Keep your final answer structured and focused on what was asked.
"""


def _read_key_docs(workspace_path: str) -> str:
    """Read key documentation files from the workspace root(s).

    Returns a formatted string with truncated doc contents suitable for
    injection into the system prompt.  This gives the LLM a high-level
    understanding of the project before it starts calling tools.
    """
    ws = Path(workspace_path).resolve()
    if not ws.is_dir():
        return ""

    found: List[str] = []
    seen_names: set = set()

    # Check both the workspace root and one level of subdirectories
    # (handles nested repos like "myapp/myapp/README.md").
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
    max_entries: int = 80,
) -> str:
    """Scan the workspace and return a compact tree + detected project roots.

    The result is injected into the system prompt so the LLM knows the
    project structure before its first tool call.
    """
    ws = Path(workspace_path).resolve()
    if not ws.is_dir():
        return ""

    tree_lines: List[str] = []
    project_roots: List[str] = []  # subdirs containing project markers

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

        # Check for project markers in this directory
        markers_here = sorted(set(filenames) & _PROJECT_MARKERS)
        if markers_here:
            rel_str = str(rel) if str(rel) != "." else "(root)"
            project_roots.append(f"  {rel_str}/ — {', '.join(markers_here)}")

        # Build tree: directories shown as "dir/", files shown individually
        indent = "  " * depth
        if depth > 0:
            tree_lines.append(f"{indent}{rel.name}/")

        # Only show files at depth 0 and 1 to keep it compact
        if depth <= 1:
            for f in sorted(filenames):
                tree_lines.append(f"{indent}  {f}")
                if len(tree_lines) >= max_entries:
                    break

        # Always show subdirectories
        for d in dirnames:
            if depth + 1 >= max_depth:
                tree_lines.append(f"{indent}  {d}/")
            # deeper dirs will be shown by subsequent walk iterations

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


def build_system_prompt(
    workspace_path: str,
    workspace_layout: Optional[str] = None,
    project_docs: Optional[str] = None,
    max_iterations: int = 25,
) -> str:
    """Build the full system prompt.

    Parameters
    ----------
    workspace_path:
        Absolute path to the workspace root.
    workspace_layout:
        Pre-computed workspace layout string (from ``scan_workspace_layout``).
        If *None*, the layout is computed on the fly.
    project_docs:
        Pre-computed project documentation string (from ``_read_key_docs``).
        If *None*, the docs are read on the fly.
    max_iterations:
        Maximum number of tool-calling iterations. Injected into the prompt
        so the LLM can budget its exploration.
    """
    if workspace_layout is None:
        workspace_layout = scan_workspace_layout(workspace_path)
    if project_docs is None:
        project_docs = _read_key_docs(workspace_path)

    docs_section = ""
    if project_docs:
        docs_section = (
            "### Project documentation (auto-detected)\n"
            "The following documentation was found in the workspace. "
            "Use it to understand the project's purpose, structure, and conventions "
            "before diving into code.\n\n"
            + project_docs
        )

    return AGENT_SYSTEM_PROMPT.format(
        workspace_path=workspace_path,
        workspace_layout_section=workspace_layout,
        project_docs_section=docs_section,
        max_iterations=max_iterations,
    )
