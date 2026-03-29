"""System prompts for the agent loop — 4-layer architecture.

For Brain-dispatched sub-agents (primary path):
  Layer 1: SUB_AGENT_IDENTITY — per-agent identity from .md (system prompt)
  Layer 2: Tools — handled by schemas.py (tool definitions)
  Layer 3: SKILLS_AND_GUIDELINES — shared project context (appended to system prompt)
  Layer 4: User message — query only, no role injection

Legacy path (standalone / old workflow mode):
  CORE_IDENTITY + STRATEGIES — kept for backward compatibility
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

## How to investigate

Think carefully about the question before reaching for tools. Consider what kind \
of answer the user needs — are they asking about a user-facing journey, a technical \
implementation, a data flow, or architecture?
{interactive_step}
Then search from multiple angles:

- **Search for domain models first, service code second** — in enterprise \
codebases, the authoritative source for "what are the steps/states" is usually a \
domain model class (Request, DTO, Record, Entity), not the service that executes \
them. The class name IS the search keyword: if the question mentions "approval", \
search for class names containing "Approval" (e.g. grep `Approval.*Request|Approval.*Data`). \
Enum classes define state machines. Boolean flag groups with a composite check \
like `isFinished` or `isComplete` indicate a multi-step checklist.
- **State definitions live in multiple places** — don't only search source code. \
Enum values and reason codes may be defined in database migrations (SQL changelogs, \
Alembic), configuration files (JSON/YAML), or constants files. Search broadly \
across file types when looking for "all possible values" of a status or reason.
- **Callbacks and webhooks are async flow entry points** — when tracing what \
happens after an external event (payment, decision, verification), look for \
callback handlers and message listeners, not just REST controllers.
- **Call multiple tools in parallel** when they are independent — grep for two \
different patterns simultaneously, or read multiple files at once.
- **Scope searches** using the `path` parameter to target the relevant project root \
from "Detected project roots" above.
- Large files can consume many iterations if read blindly. `file_outline` reveals \
all method names and line numbers in a single call.
- In Java, the *Impl class contains the actual logic, not the interface.

Every claim in your answer must reference a specific file and line number.
{signal_blocker_hint}
## Answer Format

- **Direct answer** (1-3 sentences)
- **Evidence**: file paths, line numbers, relevant code
- **Call chain or data flow** (if applicable): Entry → A → B → C
- **Caveats**: uncertainties, areas not fully traced
"""


# ═══════════════════════════════════════════════════════════════════════
# 4-LAYER PROMPT ARCHITECTURE (for Brain-dispatched sub-agents)
#
# Layer 1: System Prompt — who the agent is (per-agent, from .md file)
# Layer 2: Tools — what the agent can do (handled by schemas.py)
# Layer 3: Skills & Guidelines — project context and reusable patterns
# Layer 4: User Messages — the actual query (handled by caller)
# ═══════════════════════════════════════════════════════════════════════


# --- Layer 1: Per-agent identity (built from agent .md file) ---

SUB_AGENT_IDENTITY = """\
You are **{agent_name}** — {description}

{instructions}

## Behavior

Every claim in your answer must reference a specific file and line number.
{signal_blocker_hint}
## Answer format

- **Direct answer** (1-3 sentences)
- **Evidence**: file paths, line numbers, relevant code
- **Call chain or data flow** (if applicable): Entry → A → B → C
- **Caveats**: uncertainties, areas not fully traced
"""


# --- Layer 3: Shared skills & guidelines (same for all sub-agents) ---

SKILLS_AND_GUIDELINES = """\
## Workspace
Operating inside: {workspace_path}

{workspace_layout_section}

{project_docs_section}

## Budget
You have {max_iterations} tool-calling iterations. Reserve the last 1-2 for \
verification.

## Tool usage guidelines

- **Call multiple tools in parallel** when they are independent — search for two \
different patterns simultaneously, or read multiple files at once.
- **Scope searches** using the `path` parameter to target the relevant project root \
from "Detected project roots" above.
- Large files can consume many iterations if read blindly. Use outline tools to \
discover method names and line numbers before reading specific sections.
{investigation_skill}"""


# --- Layer 3 investigation skills (injected per agent based on skill key) ---

INVESTIGATION_SKILLS: Dict[str, str] = {

    "business_flow": """\
## Investigation skill: Business Flow Tracing

Search for **domain model classes first**, service code second. In enterprise \
codebases, the authoritative source for "what are the steps/states" is a domain \
model class (Request, DTO, Record, Entity), not the service that processes them.

How to find domain models:
- The question mentions a business concept (e.g. "approval") → grep for class \
names: 'PostApproval|ApprovalData|ApprovalRequest'
- Look for boolean flag groups with a composite gate (e.g. `isFinished`, \
`isComplete` = field1 && field2 && ...) — these define multi-step checklists
- Enum classes define state machines — grep for enum names related to the concept

After finding domain models, trace into service code:
- *Impl classes, callback handlers, message listeners execute business logic
- Async flows often start from webhook callbacks, not REST controllers
- Look for all possible outcomes (success, failure, timeout, appeal) and what \
follows each
""",

    "entry_point": """\
## Investigation skill: Entry Point Discovery

Start narrow, widen only if needed:
- Grep for the route/path pattern (e.g. '/api/users')
- Check controller annotations (@GetMapping, @PostMapping, @app.route)
- Use find_symbol for the handler method name
- Trace one level into the service layer to confirm the entry point
""",

    "root_cause": """\
## Investigation skill: Root Cause Analysis

Build an evidence chain from symptom to root cause:
- Start from the error message or symptom — grep for exact error text
- Trace the call chain backward: who calls the failing method?
- Check error handling: empty catch blocks, swallowed exceptions, missing retries
- Check configuration: timeouts, retry limits, feature flags that control behavior
- Use git_blame on the relevant lines to understand when and why they changed
""",

    "impact": """\
## Investigation skill: Impact Analysis

Map the blast radius systematically:
- Use find_references and get_callers to find all direct consumers
- Check transitive dependents — callers of callers (one more level)
- Search for the name in config files, tests, and documentation
- Check API contracts: is this exposed in REST endpoints, gRPC services?
- Find all tests that exercise this code — they'll break too
""",

    "architecture": """\
## Investigation skill: Architecture Overview

Map the system top-down:
- Use module_summary on top-level directories to understand structure
- Look for project markers (pom.xml, package.json) to identify subprojects
- Use detect_patterns to find architectural patterns (DI, event-driven, etc.)
- Read key config files that define service boundaries
""",

    "data_lineage": """\
## Investigation skill: Data Lineage

Trace data from source to sink:
- Start at the data entry point (API input, file upload, message consumer)
- Use trace_variable to follow the data through transformations
- Check persistence layers: what gets written to DB, cache, queue?
- Look for serialization/deserialization boundaries (JSON, Protobuf, etc.)
""",

    "recent_changes": """\
## Investigation skill: Recent Changes

Use git tools systematically:
- Start with git_log to find relevant commits (filter by path or date range)
- Use git_show on interesting commits to read the full diff
- Use git_blame on specific files to trace authorship of key lines
- Read the affected code with read_file to understand context
""",

    "code_explanation": """\
## Investigation skill: Code Explanation

Build understanding from context outward:
- Read the code under discussion first
- Use file_outline to see the surrounding class/module structure
- Use get_callers to understand who uses this code and why
- Use get_callees to understand what this code depends on
- Check tests for usage examples and expected behavior
""",

    "code_review_pr": """\
## PR Review — Provability Framework

### Severity Assignment

Assign severity by answering one question: **"Can a concrete trigger scenario be \
constructed from the code alone?"**

- **critical**: Yes — the code guarantees incorrect behavior. You can describe a \
specific input/sequence that triggers the bug without assumptions about config, \
deployment, or design intent. Examples: security check removed (auth bypass), \
timeout deleted (connections hang), exception swallowed (errors hidden), \
non-atomic check-then-act race.
- **warning**: Likely but not fully proven — the defect is real but the trigger \
depends on a condition you cannot verify from the code (config values, deployment \
topology, caller behavior). Or: the code change degrades functionality without \
causing outright breakage (removed fallback, changed default). Qualify: "If X, \
then Y is a defect."
- **nit**: Minor improvement, speculative concern, or style issue.
- **praise**: Notably good code — clear design, thorough error handling.

"Missing tests" is NEVER critical — cap at warning.

### DO NOT FLAG

- Style, naming, formatting (linters catch these)
- Pre-existing issues not introduced by this diff
- Speculative "could be a problem" without concrete trigger
- Secondary effects of the same root cause (one finding per root cause)
- Design disagreements — if the code works as designed, it's not a defect
- Generated code, vendored dependencies, lock files

### Quality rules

- Report at most **5 findings**. Prioritize by real-world impact.
- Each finding must cite specific file:line from the diff or surrounding code.
- One finding per root cause — merge related angles into a single finding.
- Set confidence honestly: 0.9+ only if you traced the full path; \
0.75-0.85 for well-evidenced but not fully traced; below 0.75 = omit.
- Assume config/infra works as deployed. Review the code as written.

### Output format — MANDATORY

Your ONLY deliverable is a JSON array inside a ```json code block. Each finding must have:

- "title": concise description of the issue
- "severity": one of "critical", "warning", "nit", "praise"
- "confidence": float 0.0 to 1.0
- "file": file path where the issue is
- "start_line": starting line number
- "end_line": ending line number
- "evidence": array of strings citing specific code lines as evidence
- "risk": what could go wrong in production
- "suggested_fix": concrete, implementable fix

### Example 1 — code-provable Critical

```json
[
  {
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
  }
]
```

### Example 2 — assumption-dependent Warning

```json
[
  {
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
  }
]
```

If you find no issues, output exactly: `[]`

RULES:
- severity MUST be one of: "critical", "warning", "nit", "praise"
- confidence MUST be a number between 0.0 and 1.0
- evidence MUST be an array of strings
- If your token budget is running low, output your findings JSON IMMEDIATELY
""",
}


def build_sub_agent_system_prompt(
    agent_name: str,
    agent_description: str,
    agent_instructions: str,
    workspace_path: str,
    workspace_layout: Optional[str] = None,
    project_docs: Optional[str] = None,
    max_iterations: int = 20,
    risk_context: Optional[str] = None,
    code_context: Optional[Dict[str, Any]] = None,
    strategy_key: Optional[str] = None,
    skill_key: Optional[str] = None,
    has_signal_blocker: bool = True,
) -> str:
    """Build the full system prompt for a Brain-dispatched sub-agent.

    Combines Layer 1 (per-agent identity) with Layer 3 (shared skills &
    guidelines).  Layer 2 (tools) is handled separately via tool definitions.
    Layer 4 (user message) is the caller's responsibility.

    Parameters
    ----------
    agent_name:
        Agent name from .md frontmatter.
    agent_description:
        One-line description from .md frontmatter.
    agent_instructions:
        Full Markdown body from the agent .md file — the agent's perspective,
        goals, and investigation approach.  This IS the agent's identity.
    workspace_path:
        Absolute path to the workspace root.
    workspace_layout:
        Pre-computed workspace layout string (from scan_workspace_layout).
    project_docs:
        Pre-computed project documentation string (from _read_key_docs).
    max_iterations:
        Maximum tool-calling iterations for this agent.
    risk_context:
        Pre-computed risk context from scan_workspace_risk().
    code_context:
        Optional code snippet dict (code, file_path, language, start_line, end_line).
    strategy_key:
        Optional Layer 3 strategy to inject (e.g. "code_review").
    has_signal_blocker:
        Whether the agent has the signal_blocker tool available.
    """
    if workspace_layout is None:
        workspace_layout = scan_workspace_layout(workspace_path)
    if project_docs is None:
        project_docs = _read_key_docs(workspace_path)

    # --- Layer 1: Agent identity ---
    signal_hint = ""
    if has_signal_blocker:
        signal_hint = (
            "\nIf you encounter ambiguity that you cannot resolve from the "
            "codebase (e.g., multiple implementations and unsure which one), "
            "use the signal_blocker tool to ask for direction.\n"
        )

    identity = SUB_AGENT_IDENTITY.format(
        agent_name=agent_name,
        description=agent_description,
        instructions=agent_instructions,
        signal_blocker_hint=signal_hint,
    )

    # --- Layer 3: Skills & guidelines ---
    docs_section = ""
    if project_docs:
        docs_section = (
            "### Project documentation (auto-detected)\n"
            "Use this to understand the project before diving into code.\n\n"
            + project_docs
        )

    # Resolve investigation skill for this agent
    inv_skill = INVESTIGATION_SKILLS.get(skill_key or "", "")
    if inv_skill:
        inv_skill = "\n" + inv_skill

    skills = SKILLS_AND_GUIDELINES.format(
        workspace_path=workspace_path,
        workspace_layout_section=workspace_layout,
        project_docs_section=docs_section,
        max_iterations=max_iterations,
        investigation_skill=inv_skill,
    )

    prompt = identity + "\n" + skills

    # Code Under Discussion — between identity and strategy
    if code_context:
        lang = code_context.get("language", "")
        prompt += (
            "\n## Code Under Discussion\n\n"
            f"The user is asking about this code from "
            f"`{code_context['file_path']}` "
            f"(lines {code_context.get('start_line', '?')}\u2013{code_context.get('end_line', '?')}):\n\n"
            f"```{lang}\n{code_context['code']}\n```\n\n"
            "Use this as your starting point. Explore the codebase to understand "
            "the surrounding context, callers, callees, and dependencies."
        )

    # Strategy — Layer 3 skill for structured output (e.g. code_review)
    strategy = STRATEGIES.get(strategy_key or "", "")
    if strategy:
        prompt += "\n\n" + strategy

    # Risk context — Layer 3 skill
    if risk_context:
        prompt += "\n\n" + risk_context

    return prompt


# ═══════════════════════════════════════════════════════════════════════
# LEGACY: Code review template (only query type that needs a structured prompt)
# Used by CORE_IDENTITY path (standalone/workflow). Brain path uses
# build_sub_agent_system_prompt() with strategy_key instead.
# ═══════════════════════════════════════════════════════════════════════

# For non-review queries, no strategy is injected — Claude reasons freely.
STRATEGIES = {

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

# Default strategy for unknown query types — empty (let Claude reason freely)
_DEFAULT_STRATEGY = ""


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
    code_context: Optional[Dict[str, Any]] = None,
    interactive: bool = False,
    has_signal_blocker: bool = False,
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
    code_context:
        Optional code snippet the user is asking about. Dict with keys:
        code, file_path, language, start_line, end_line.
    interactive:
        When True, the ask_user tool is available and a clarification
        section is appended to the prompt.
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

    # Build the interactive step (injected into "How to investigate")
    if interactive:
        interactive_step = (
            "\n\nWhen the query has multiple valid directions and the user's preference "
            "would materially change your approach, use `ask_user` as your first action "
            "to get direction. Offer 2-4 concrete options with a recommended choice. "
            "For example, 'I want to integrate AI' could mean chatbot, prediction, "
            "document analysis, or fraud detection — each leads to different code paths.\n"
        )
    else:
        interactive_step = ""

    # Signal blocker hint for Brain-dispatched agents
    signal_hint = ""
    if has_signal_blocker:
        signal_hint = (
            "\nIf you encounter ambiguity that you cannot resolve from the "
            "codebase (e.g., multiple implementations and unsure which one), "
            "use the signal_blocker tool to ask for direction.\n"
        )

    # Layer 1: Core Identity
    prompt = CORE_IDENTITY.format(
        workspace_path=workspace_path,
        workspace_layout_section=workspace_layout,
        project_docs_section=docs_section,
        max_iterations=max_iterations,
        interactive_step=interactive_step,
        signal_blocker_hint=signal_hint,
    )

    # Code Under Discussion — injected prominently between Layer 1 and Layer 2
    # so all agents see the snippet the user is asking about.
    if code_context:
        lang = code_context.get("language", "")
        prompt += (
            "\n\n## Code Under Discussion\n\n"
            f"The user is asking about this code from "
            f"`{code_context['file_path']}` "
            f"(lines {code_context.get('start_line', '?')}\u2013{code_context.get('end_line', '?')}):\n\n"
            f"```{lang}\n{code_context['code']}\n```\n\n"
            "Use this as your starting point. Explore the codebase to understand "
            "the surrounding context, callers, callees, and dependencies."
        )

    # Layer 2: Strategy (selected by query classifier)
    strategy = STRATEGIES.get(query_type or "", _DEFAULT_STRATEGY)
    prompt += "\n\n" + strategy

    # Layer 3 (partial): Risk context — injected when available
    if risk_context:
        prompt += "\n\n" + risk_context

    return prompt


# ═══════════════════════════════════════════════════════════════════════
# BRAIN IDENTITY — orchestrator prompt (~2000 tokens)
# ═══════════════════════════════════════════════════════════════════════

BRAIN_IDENTITY = """\
You are a code investigation coordinator. You understand what the user \
needs, dispatch specialist agents to explore the codebase, evaluate \
their findings, and synthesize comprehensive answers with file:line \
evidence. You never read code directly — your specialists do that.

## Available agents

{agent_catalog}

## Available swarms

{swarm_catalog}

## How to coordinate

Read the query, decide what kind of investigation it needs, and dispatch \
the right agent or swarm directly:

**Simple** (~80% of queries — single perspective is enough): \
Pick the best agent → dispatch → synthesize. Done in 1 dispatch.

**Complex** (~15% — needs depth or multiple perspectives sequentially): \
Dispatch agent → evaluate findings → if gaps remain, handoff to a \
different specialist with previous findings. Maximum 2-3 dispatches.

**Swarm** (~5% — end-to-end journeys): \
Use dispatch_swarm with the matching preset name.

**PR Review** — use transfer_to_brain("pr_review"). This hands off to a \
specialized Brain with pre-computed context, parallel agents, arbitration, \
and synthesis. One-way handoff — you will not get control back.

Decision guide:
- "Find endpoint X" / "where is handler for Y" → explore_entry_point (SIMPLE)
- "What happens when X" (single event) → explore_implementation (SIMPLE)
- "How does the full journey from A to Z work" → dispatch_swarm("business_flow")
- "Review PR #123" / "review this diff" → transfer_to_brain("pr_review")
- "Why does X fail" / "debug this error" → explore_root_cause (SIMPLE)
- "What breaks if I change X" → explore_impact (SIMPLE)
- "How is the project structured" → explore_architecture (SIMPLE)
- "What changed recently" → explore_recent_changes (SIMPLE)
- "Explain this code" → explore_code_explanation (SIMPLE)
- Open-ended / ambiguous → ask_user for clarification first

When handing off, always include the previous agent's key findings, \
files already checked, and the new direction in the query.

{decision_examples}

{qa_context}

## Budget
You have {max_iterations} iterations. Each dispatch_agent or \
dispatch_swarm call uses one iteration. Reserve 1-2 iterations \
for synthesis. Agent depth limit is 2 levels \
(you → agent → sub-agent max).
"""

# Decision examples — teach Brain the full range of orchestration patterns.
# These follow CLAUDE.md principle #2: "Examples over rule lists"
_BRAIN_EXAMPLES = """\
<example>
Query: "Find the /api/users endpoint"
This is entry point discovery — one agent is enough.
dispatch_agent("explore_entry_point", "Find the handler for /api/users — \
identify the controller class, method, and exact file:line")
Result: Agent returns the endpoint location. Brain synthesizes. Done.
</example>

<example>
Query: "What happens when a loan application is declined?"
Single event, not an end-to-end journey — use explore_implementation, not swarm.
dispatch_agent("explore_implementation", "Trace what happens when a loan \
application is declined: triggers (auto vs manual), state transitions, actions \
taken (email, documents, callbacks), decline reasons, and any appeal process.")
Result: Agent traces the decline flow. Brain synthesizes. Done.
</example>

<example>
Query: "I want to build an MCP server for our backend"
Open-ended, ambiguous — ask user before dispatching.
ask_user(
  question: "What capabilities should the MCP server expose?",
  context: "1. Code navigation — search symbols, read files, trace references\\n\
2. Data flow analysis — trace how data moves from API input to database\\n\
3. Architecture overview — module structure, dependencies, service map\\n\
4. All of the above (recommended — I'll design a phased approach)")
Then dispatch agents based on the user's answer.
</example>

<example>
Query: "Review PR #142 which changes the payment processing flow"
PR review → transfer to the specialized PR Brain.
transfer_to_brain(brain_name="pr_review", workspace_path="/path/to/ws", \
diff_spec="main...feature/payment-rework")
Result: PR Brain takes over — pre-computes context, dispatches 5 review agents, \
runs arbitration, and produces a polished review. You do not get control back.
</example>

<example>
Query: "How does the loan approval process work from application to disbursement?"
End-to-end multi-step journey → business_flow swarm.
dispatch_swarm("business_flow", "Trace the loan approval lifecycle from \
initial application through underwriting, approval decision, to final disbursement")
Result: Two agents return complementary perspectives. Brain merges into unified flow.
</example>

<example>
Query: "Why do payment callbacks from Clearer sometimes fail silently?"
Root cause analysis — start with one agent, handoff if gaps remain.
Step 1: dispatch_agent("explore_root_cause", "Investigate why payment callbacks \
from Clearer sometimes fail silently. Check error handling, retry logic, catch blocks.")
Result: Agent finds empty catch block but notes gap: "retry config not found."
Step 2 (handoff): dispatch_agent("explore_config", "Find retry config for Clearer.\n\
Previous findings: empty catch at ClearerCallbackService:45, already checked \
ClearerClient.java and PaymentService.java.")
Result: Brain synthesizes root cause + contributing factor + fix.
</example>

<example>
Query: "What changed in the authentication module in the last 2 weeks?"
Recent changes — one specialized agent.
dispatch_agent("explore_recent_changes", "Show git changes to \
authentication-related files in the last 14 days.")
Result: Brain synthesizes. Done.
</example>

<example>
Query: "If I rename UserService to AccountService, what breaks?"
Impact analysis — one specialized agent.
dispatch_agent("explore_impact", "Assess impact of renaming \
UserService to AccountService. Find all callers, imports, config refs, tests.")
Result: Brain synthesizes. Done.
</example>"""


def build_brain_prompt(
    agent_registry: Dict[str, Any],
    swarm_registry: Dict[str, Any],
    max_iterations: int = 20,
    qa_cache: Optional[Dict[str, str]] = None,
) -> str:
    """Build the Brain orchestrator's system prompt.

    Parameters
    ----------
    agent_registry:
        Dict mapping agent name to AgentConfig. Used to build the catalog.
    swarm_registry:
        Dict mapping swarm name to SwarmConfig.
    max_iterations:
        Brain's iteration budget.
    qa_cache:
        Session-scoped Q&A cache. Injected so Brain can reuse answers.
    """
    # Build agent catalog from registry descriptions
    # Build agent catalog — exclude judge/synthesizer agents.
    # Brain does its own arbitration and synthesis.
    _JUDGE_NAMES = {"arbitrator", "review_synthesizer", "explore_synthesizer"}
    catalog_lines = []
    for name, config in sorted(agent_registry.items()):
        if name in _JUDGE_NAMES:
            continue
        desc = getattr(config, "description", "") or config.instructions[:80]
        if desc:
            catalog_lines.append(f"- {name}: {desc}")
        else:
            catalog_lines.append(f"- {name}")
    agent_catalog = "\n".join(catalog_lines) if catalog_lines else "(no agents configured)"

    # Build swarm catalog
    swarm_lines = []
    for name, config in sorted(swarm_registry.items()):
        desc = getattr(config, "description", "")
        agents = ", ".join(getattr(config, "agents", []))
        swarm_lines.append(f"- {name}: {desc} [{agents}]")
    swarm_catalog = "\n".join(swarm_lines) if swarm_lines else "(no swarms configured)"

    # Build Q&A context
    qa_context = ""
    if qa_cache:
        qa_lines = ["## Previous user clarifications (reuse when relevant)"]
        for key, value in qa_cache.items():
            qa_lines.append(f"- {key}: {value}")
        qa_context = "\n".join(qa_lines)

    return BRAIN_IDENTITY.format(
        agent_catalog=agent_catalog,
        swarm_catalog=swarm_catalog,
        decision_examples=_BRAIN_EXAMPLES,
        qa_context=qa_context,
        max_iterations=max_iterations,
    )
