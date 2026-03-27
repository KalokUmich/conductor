"""Pydantic schemas for code intelligence tools."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Tool parameter schemas
# ---------------------------------------------------------------------------


class GrepParams(BaseModel):
    pattern: str = Field(..., description="Regex pattern to search for.")
    path: Optional[str] = Field(None, description="Relative path within workspace to search (file or directory).")
    include_glob: Optional[str] = Field(None, description="Glob to filter files by extension, e.g. '*.java', '*.py'. Omit to search all files.")
    max_results: int = Field(default=50, ge=1, le=200)


class ReadFileParams(BaseModel):
    path: str = Field(..., description="Relative file path within workspace.")
    start_line: Optional[int] = Field(None, ge=1, description="First line to read (1-based).")
    end_line: Optional[int] = Field(None, ge=1, description="Last line to read (1-based, inclusive).")


class ListFilesParams(BaseModel):
    directory: str = Field(default=".", description="Relative directory within workspace.")
    max_depth: Optional[int] = Field(default=3, ge=1, le=10)
    include_glob: Optional[str] = Field(None, description="Glob to filter, e.g. '*.py'.")


class FindSymbolParams(BaseModel):
    name: str = Field(..., description="Symbol name to find (exact or substring).")
    kind: Optional[str] = Field(None, description="Symbol kind filter: function, class, method, interface, type.")


class FindReferencesParams(BaseModel):
    symbol_name: str = Field(..., description="Symbol name to find references for.")
    file: Optional[str] = Field(None, description="Limit search to this relative file path.")


class FileOutlineParams(BaseModel):
    path: str = Field(..., description="Relative file path within workspace.")


class GetDependenciesParams(BaseModel):
    file_path: str = Field(..., description="Relative file path to find dependencies of.")
    max_depth: int = Field(default=1, ge=1, le=3, description="Traversal depth: 1=direct only, 2-3=transitive.")


class GetDependentsParams(BaseModel):
    file_path: str = Field(..., description="Relative file path to find dependents of.")
    max_depth: int = Field(default=1, ge=1, le=3, description="Traversal depth: 1=direct only, 2-3=transitive.")


class GitLogParams(BaseModel):
    file: Optional[str] = Field(None, description="Relative file path to filter log.")
    n: int = Field(default=10, ge=1, le=50, description="Number of commits to show.")
    search: Optional[str] = Field(None, description="Search commit messages for this text (git log --grep).")


class GitDiffParams(BaseModel):
    ref1: Optional[str] = Field(default="HEAD~1", description="First git ref.")
    ref2: Optional[str] = Field(default="HEAD", description="Second git ref.")
    file: Optional[str] = Field(None, description="Limit diff to this file.")
    context_lines: int = Field(
        default=10, ge=0, le=50,
        description="Number of surrounding context lines in the diff (default 10).",
    )


class GitDiffFilesParams(BaseModel):
    ref: str = Field(
        ...,
        description=(
            "Git diff specification. Examples: "
            "'master...feature/xxx' (PR diff — changes since branch point), "
            "'master..feature/xxx' (commit range), "
            "'HEAD~5' (last 5 commits vs working tree), "
            "'abc1234 def5678' (between two commits)."
        ),
    )


class AstSearchParams(BaseModel):
    pattern: str = Field(..., description="ast-grep pattern (e.g. 'def $F($$$ARGS)', 'if $COND: $$$BODY').")
    language: Optional[str] = Field(None, description="Language hint: python, javascript, typescript, go, rust, java, c, cpp.")
    path: Optional[str] = Field(None, description="Relative path within workspace to search (file or directory).")
    max_results: int = Field(default=30, ge=1, le=100)


class GetCalleesParams(BaseModel):
    function_name: str = Field(..., description="Name of the function to inspect.")
    file: str = Field(..., description="Relative file path containing the function.")


class GetCallersParams(BaseModel):
    function_name: str = Field(..., description="Name of the function to find callers of.")
    path: Optional[str] = Field(None, description="Relative path to limit the search.")


class GitBlameParams(BaseModel):
    file: str = Field(..., description="Relative file path within workspace.")
    start_line: Optional[int] = Field(None, ge=1, description="First line to blame (1-based).")
    end_line: Optional[int] = Field(None, ge=1, description="Last line to blame (1-based, inclusive).")


class GitShowParams(BaseModel):
    commit: str = Field(..., description="Commit hash (short or full) to show.")
    file: Optional[str] = Field(None, description="Limit diff to this relative file path.")


class FindTestsParams(BaseModel):
    name: str = Field(..., description="Function or class name to find tests for.")
    path: Optional[str] = Field(None, description="Relative path to limit the test search.")


class TestOutlineParams(BaseModel):
    path: str = Field(..., description="Relative path to a test file.")


class TraceVariableParams(BaseModel):
    variable_name: str = Field(..., description="Name of the variable to trace (e.g. 'loan_id').")
    file: str = Field(..., description="Relative file path containing the variable.")
    function_name: Optional[str] = Field(
        None,
        description="Function containing the variable. If omitted, the first function referencing it is used.",
    )
    direction: str = Field(
        default="forward",
        description=(
            "'forward' = trace where the value flows to (call sites, ORM/SQL sinks). "
            "'backward' = trace where the value comes from (callers, HTTP/config sources)."
        ),
    )


class CompressedViewParams(BaseModel):
    file_path: str = Field(..., description="Relative path to the file to analyze.")
    focus: Optional[str] = Field(
        None,
        description="Optional: focus on a specific symbol name (substring match).",
    )


class ModuleSummaryParams(BaseModel):
    module_path: str = Field(..., description="Relative path to the module directory (e.g. 'app/auth').")


class ExpandSymbolParams(BaseModel):
    symbol_name: str = Field(..., description="Name of the symbol to expand (e.g. 'PaymentService' or 'process_payment').")
    file_path: Optional[str] = Field(
        None,
        description="File containing the symbol. If omitted, searches the workspace.",
    )


class DetectPatternsParams(BaseModel):
    path: Optional[str] = Field(
        None,
        description="Relative path within workspace to scan (file or directory). Omit to scan the whole workspace.",
    )
    categories: Optional[List[str]] = Field(
        None,
        description=(
            "Pattern categories to detect. Omit to detect all. "
            "Options: webhook, queue, retry, lock, check_then_act, "
            "transaction, token_lifecycle, side_effect_chain."
        ),
    )
    max_results: int = Field(default=50, ge=1, le=200)


class RunTestParams(BaseModel):
    test_file: str = Field(
        ...,
        description="Relative path to the test file to run (e.g. 'tests/test_auth.py').",
    )
    test_name: Optional[str] = Field(
        None,
        description=(
            "Specific test function or class to run (e.g. 'test_timeout', "
            "'TestAuth::test_login'). If omitted, runs the whole file."
        ),
    )
    timeout: int = Field(
        default=30, ge=5, le=60,
        description="Max seconds to wait for the test run (default: 30).",
    )


# ---------------------------------------------------------------------------
# New analysis tool parameter schemas
# ---------------------------------------------------------------------------


class GitHotspotsParams(BaseModel):
    days: int = Field(default=90, ge=7, le=365, description="Look-back window in days.")
    top_n: int = Field(default=15, ge=1, le=50, description="Max hotspot files to return.")


class ListEndpointsParams(BaseModel):
    path: Optional[str] = Field(None, description="Relative path to scope the scan (file or directory).")
    max_results: int = Field(default=100, ge=1, le=500)


class ExtractDocstringsParams(BaseModel):
    path: str = Field(..., description="Relative file path to extract docstrings from.")
    symbol_name: Optional[str] = Field(None, description="Only extract docstring for this symbol.")


class DbSchemaParams(BaseModel):
    path: Optional[str] = Field(None, description="Relative path to scope the scan. Omit for whole workspace.")
    max_results: int = Field(default=50, ge=1, le=200)


# ---------------------------------------------------------------------------
# Interactive tool parameter schemas
# ---------------------------------------------------------------------------


class AskUserParams(BaseModel):
    question: str = Field(..., description="The clarifying question to ask the user. Be specific about what information you need.")
    context: str = Field(
        default="",
        description="Brief context for why you need this information, shown to the user alongside the question.",
    )


# ---------------------------------------------------------------------------
# Brain orchestrator tool parameter schemas
# ---------------------------------------------------------------------------


class SignalBlockerParams(BaseModel):
    reason: str = Field(..., description="Why you need direction — describe what ambiguity or choice you encountered.")
    options: List[str] = Field(default_factory=list, description="2-4 concrete options you've identified.")
    context: str = Field(default="", description="Brief context about what you've found so far.")


class DispatchAgentParams(BaseModel):
    agent_name: str = Field(..., description="Agent to dispatch (from the available agents list in your system prompt)")
    query: str = Field(..., description="Focused question for the agent to investigate")
    budget_weight: float = Field(default=1.0, ge=0.3, le=2.0, description="Budget multiplier (1.0 = standard)")


class DispatchSwarmParams(BaseModel):
    swarm_name: str = Field(..., description="Swarm preset name (e.g. 'pr_review', 'business_flow'). Only use predefined swarms.")
    query: str = Field(..., description="Shared investigation query for all agents in the swarm")


# ---------------------------------------------------------------------------
# Browser tool parameter schemas
# ---------------------------------------------------------------------------


class WebSearchParams(BaseModel):
    query: str = Field(..., description="Search query (e.g. 'playwright timeout error', 'fastapi lifespan example').")
    max_results: int = Field(default=10, ge=1, le=20, description="Max number of results to return.")


class WebNavigateParams(BaseModel):
    url: str = Field(..., description="URL to navigate to (must start with http:// or https://).")
    wait_until: str = Field(
        default="domcontentloaded",
        description="When to consider navigation succeeded: 'load', 'domcontentloaded', or 'networkidle'.",
    )


class WebClickParams(BaseModel):
    selector: Optional[str] = Field(
        None,
        description="CSS selector of the element to click (e.g. 'button.submit', '#login').",
    )
    text: Optional[str] = Field(
        None,
        description="Click the element containing this exact text (uses getByText).",
    )


class WebFillParams(BaseModel):
    selector: str = Field(
        ...,
        description="CSS selector of the input field to fill (e.g. 'input[name=email]', '#search').",
    )
    value: str = Field(..., description="Text value to type into the field.")
    press_enter: bool = Field(
        default=False,
        description="Press Enter after filling the field (useful for search boxes).",
    )


class WebScreenshotParams(BaseModel):
    selector: Optional[str] = Field(
        None,
        description="CSS selector to screenshot. Omit for full page.",
    )
    full_page: bool = Field(
        default=True,
        description="Capture the full scrollable page (ignored if selector is set).",
    )


class WebExtractParams(BaseModel):
    selector: str = Field(
        ...,
        description="CSS selector to extract content from (e.g. 'table', '.article-body', 'h1').",
    )
    attribute: Optional[str] = Field(
        None,
        description="Extract this HTML attribute instead of text content (e.g. 'href', 'src').",
    )
    max_results: int = Field(default=20, ge=1, le=100, description="Max elements to return.")


# ---------------------------------------------------------------------------
# Tool name → Pydantic param model mapping
#
# Used by execute_tool() to validate and coerce raw LLM params before
# dispatching.  Pydantic v2 coerces e.g. "240" → int(240) automatically,
# which fixes non-Claude models that return numbers as strings.
# ---------------------------------------------------------------------------

TOOL_PARAM_MODELS: Dict[str, type] = {
    "grep": GrepParams,
    "read_file": ReadFileParams,
    "list_files": ListFilesParams,
    "find_symbol": FindSymbolParams,
    "find_references": FindReferencesParams,
    "file_outline": FileOutlineParams,
    "get_dependencies": GetDependenciesParams,
    "get_dependents": GetDependentsParams,
    "git_log": GitLogParams,
    "git_diff": GitDiffParams,
    "git_diff_files": GitDiffFilesParams,
    "ast_search": AstSearchParams,
    "get_callees": GetCalleesParams,
    "get_callers": GetCallersParams,
    "git_blame": GitBlameParams,
    "git_show": GitShowParams,
    "find_tests": FindTestsParams,
    "test_outline": TestOutlineParams,
    "trace_variable": TraceVariableParams,
    "compressed_view": CompressedViewParams,
    "module_summary": ModuleSummaryParams,
    "expand_symbol": ExpandSymbolParams,
    "detect_patterns": DetectPatternsParams,
    "run_test": RunTestParams,
    # New analysis tools
    "git_hotspots": GitHotspotsParams,
    "list_endpoints": ListEndpointsParams,
    "extract_docstrings": ExtractDocstringsParams,
    "db_schema": DbSchemaParams,
    # Browser tools
    "web_search": WebSearchParams,
    "web_navigate": WebNavigateParams,
    "web_click": WebClickParams,
    "web_fill": WebFillParams,
    "web_screenshot": WebScreenshotParams,
    "web_extract": WebExtractParams,
    # Interactive tools
    "ask_user": AskUserParams,
    # Brain orchestrator tools
    "dispatch_agent": DispatchAgentParams,
    "dispatch_swarm": DispatchSwarmParams,
    "signal_blocker": SignalBlockerParams,
}


# ---------------------------------------------------------------------------
# Tool result schemas
# ---------------------------------------------------------------------------


class GrepMatch(BaseModel):
    file_path: str
    line_number: int
    content: str


class SymbolLocation(BaseModel):
    name: str
    kind: str
    file_path: str
    start_line: int
    end_line: int
    signature: str = ""


class ReferenceLocation(BaseModel):
    file_path: str
    line_number: int
    content: str


class FileEntry(BaseModel):
    path: str
    is_dir: bool
    size: Optional[int] = None


class AstMatch(BaseModel):
    file_path: str
    start_line: int
    end_line: int
    text: str
    meta_variables: Dict[str, str] = Field(default_factory=dict)


class CallerInfo(BaseModel):
    caller_name: str
    caller_kind: str  # "function", "method", "class"
    file_path: str
    line: int
    content: str


class CalleeInfo(BaseModel):
    callee_name: str
    file_path: str
    line: int


class DependencyInfo(BaseModel):
    file_path: str
    symbols: List[str] = Field(default_factory=list)
    weight: int = 1


class GitCommit(BaseModel):
    hash: str
    message: str
    author: str = ""
    date: str = ""


class DiffFileEntry(BaseModel):
    path: str
    status: str  # "added", "modified", "deleted", "renamed", "copied"
    additions: int = 0
    deletions: int = 0
    old_path: Optional[str] = None  # for renames


class BlameEntry(BaseModel):
    commit_hash: str
    author: str
    date: str
    line_number: int
    content: str


class TestMatch(BaseModel):
    test_file: str
    test_function: str
    line_number: int
    context: str = ""


class TestOutlineEntry(BaseModel):
    name: str
    kind: str  # "test_function", "test_class", "describe_block", "it_block"
    line_number: int
    end_line: int = 0
    mocks: List[str] = Field(default_factory=list)
    assertions: List[str] = Field(default_factory=list)
    fixtures: List[str] = Field(default_factory=list)


class ToolResult(BaseModel):
    """Unified tool result wrapper."""
    tool_name: str
    success: bool = True
    data: Any = None
    error: Optional[str] = None
    truncated: bool = False


# ---------------------------------------------------------------------------
# Tool definition schema (for LLM tool_use protocol)
# ---------------------------------------------------------------------------

def filter_tools(names: List[str]) -> List[Dict[str, Any]]:
    """Return TOOL_DEFINITIONS filtered to only the given tool names."""
    name_set = set(names)
    return [t for t in TOOL_DEFINITIONS if t["name"] in name_set]


def get_ask_user_tool_def() -> Dict[str, Any]:
    """Return the ask_user tool definition dict (for interactive mode injection)."""
    return next(t for t in TOOL_DEFINITIONS if t["name"] == "ask_user")


TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "name": "grep",
        "description": (
            "Search for a regex pattern across files in the workspace. "
            "Returns matching lines with file paths and line numbers. "
            "Use path to scope search to a subdirectory. "
            "Only use include_glob if you know the exact file extension (e.g. '*.java', '*.py'). "
            "Omit include_glob to search ALL file types."
        ),
        "input_schema": GrepParams.model_json_schema(),
    },
    {
        "name": "read_file",
        "description": (
            "Read file contents. Supports line ranges for reading specific sections. "
            "Use start_line/end_line to read a portion of a large file."
        ),
        "input_schema": ReadFileParams.model_json_schema(),
    },
    {
        "name": "list_files",
        "description": (
            "List files and directories in the workspace. "
            "Use max_depth to control how deep to recurse. "
            "Use include_glob to filter by pattern."
        ),
        "input_schema": ListFilesParams.model_json_schema(),
    },
    {
        "name": "find_symbol",
        "description": (
            "Find symbol definitions (functions, classes, methods, interfaces) by name using AST parsing. "
            "Returns exact file locations with line numbers and signatures. "
            "More precise than grep for finding where something is defined."
        ),
        "input_schema": FindSymbolParams.model_json_schema(),
    },
    {
        "name": "find_references",
        "description": (
            "Find all references (usages) of a symbol across the codebase. "
            "Combines grep with AST validation for accurate results."
        ),
        "input_schema": FindReferencesParams.model_json_schema(),
    },
    {
        "name": "file_outline",
        "description": (
            "Get the structure of a file: all classes, functions, methods with line numbers. "
            "Useful for understanding a file's organization before reading specific sections."
        ),
        "input_schema": FileOutlineParams.model_json_schema(),
    },
    {
        "name": "get_dependencies",
        "description": (
            "Find what files a given file depends on (imports/references). "
            "Uses the dependency graph. Set max_depth=2 or 3 to find transitive "
            "dependencies (A imports B imports C). Each result includes its depth."
        ),
        "input_schema": GetDependenciesParams.model_json_schema(),
    },
    {
        "name": "get_dependents",
        "description": (
            "Find what files depend on a given file (reverse dependencies). "
            "Set max_depth=2 or 3 to find transitive dependents (who depends on "
            "files that depend on this file). Useful for blast radius analysis."
        ),
        "input_schema": GetDependentsParams.model_json_schema(),
    },
    {
        "name": "git_log",
        "description": (
            "Show recent git commits, optionally filtered to a specific file. "
            "Use search= to find commits mentioning specific terms (e.g. 'CVE', "
            "'timeout', 'fix'). Useful for understanding what changed recently "
            "and finding security-related or bug-fix commits."
        ),
        "input_schema": GitLogParams.model_json_schema(),
    },
    {
        "name": "git_diff",
        "description": (
            "Show the full unified diff between two git refs (commits, branches). "
            "Use file parameter to limit diff to a single file. "
            "For large PRs, prefer git_diff_files first to see what changed, "
            "then git_diff with file= for each file you want to review."
        ),
        "input_schema": GitDiffParams.model_json_schema(),
    },
    {
        "name": "git_diff_files",
        "description": (
            "List files changed between two git refs with status and line counts. "
            "Returns a structured list: path, status (added/modified/deleted/renamed), "
            "additions, deletions. Supports three-dot syntax for PR diffs: "
            "'master...feature/xxx'. Use this FIRST in code review to get an overview, "
            "then use git_diff with file= to review individual files."
        ),
        "input_schema": GitDiffFilesParams.model_json_schema(),
    },
    {
        "name": "ast_search",
        "description": (
            "Structural AST search using ast-grep patterns. "
            "More precise than regex grep — matches code structure, not text. "
            "Use $VAR for single nodes, $$$VAR for multiple nodes. "
            "Examples: 'def $F($$$ARGS)', 'if $COND: $$$BODY', '$OBJ.$METHOD($$$ARGS)'."
        ),
        "input_schema": AstSearchParams.model_json_schema(),
    },
    {
        "name": "get_callees",
        "description": (
            "Find all functions/methods called within a specific function body. "
            "Requires the function name and file path. "
            "ESSENTIAL for tracing business flows: after finding an entry point, "
            "call get_callees to discover ALL downstream services it invokes "
            "(e.g. email, payment, verification). This reveals the complete "
            "chain of steps without reading the entire file."
        ),
        "input_schema": GetCalleesParams.model_json_schema(),
    },
    {
        "name": "get_callers",
        "description": (
            "Find all functions/methods that call a given function. "
            "Searches across the entire codebase (or a specific path). "
            "Useful for understanding impact and usage patterns."
        ),
        "input_schema": GetCallersParams.model_json_schema(),
    },
    {
        "name": "git_blame",
        "description": (
            "Run git blame on a file to see who last changed each line, with commit hash, "
            "author, and date. Optionally limit to a line range. "
            "Use this to trace when and by whom specific code was introduced or modified. "
            "Follow up with git_show on interesting commit hashes to understand WHY."
        ),
        "input_schema": GitBlameParams.model_json_schema(),
    },
    {
        "name": "git_show",
        "description": (
            "Show full details of a specific git commit: author, date, full commit message "
            "(including body/PR description), and the diff. "
            "Use after git_log or git_blame to understand the motivation behind a change."
        ),
        "input_schema": GitShowParams.model_json_schema(),
    },
    {
        "name": "find_tests",
        "description": (
            "Find test functions that test a given function or class. "
            "Searches test files (test_*.py, *_test.py, *.test.ts, *.spec.ts, *_test.go, *Test.java, *_test.rs) "
            "for references to the target and returns the enclosing test function with context. "
            "Useful for understanding test coverage and finding relevant test examples."
        ),
        "input_schema": FindTestsParams.model_json_schema(),
    },
    {
        "name": "test_outline",
        "description": (
            "Get the detailed structure of a test file: test classes/suites, test functions, "
            "what they mock (patch/MagicMock/jest.fn/vi.mock), what they assert, and fixtures used. "
            "Richer than file_outline — understands test semantics for pytest, jest, mocha, vitest, and Go."
        ),
        "input_schema": TestOutlineParams.model_json_schema(),
    },
    {
        "name": "trace_variable",
        "description": (
            "Trace a variable's data flow through function calls. "
            "Forward: finds where the value goes — aliases, function call argument-to-parameter mapping, "
            "and sinks (ORM filters, SQL parameters, HTTP bodies, return statements). "
            "Backward: finds where the value comes from — callers that pass this parameter, "
            "and sources (HTTP requests, config, DB results). "
            "Use this to answer 'how does loan_id flow from the HTTP request into the SQL WHERE clause?' "
            "by chaining forward hops across function boundaries."
        ),
        "input_schema": TraceVariableParams.model_json_schema(),
    },
    {
        "name": "compressed_view",
        "description": (
            "Return a compressed view of a file showing function/class signatures, "
            "call relationships, side effects, and exceptions. Saves ~80% tokens vs "
            "read_file. Use this FIRST to understand a file's structure, then use "
            "expand_symbol only for specific symbols you need in detail."
        ),
        "input_schema": CompressedViewParams.model_json_schema(),
    },
    {
        "name": "module_summary",
        "description": (
            "Return a high-level summary of a module/directory: key services, models, "
            "classes, functions, dependencies, and file list. Saves ~95% tokens vs "
            "reading all files. Use this when deciding which part of the codebase to "
            "explore, or when answering architecture questions."
        ),
        "input_schema": ModuleSummaryParams.model_json_schema(),
    },
    {
        "name": "expand_symbol",
        "description": (
            "Expand a symbol to its full source code. Use after compressed_view when "
            "you need the complete implementation of a specific function or class. "
            "Provide file_path for faster lookup, or omit to search the workspace."
        ),
        "input_schema": ExpandSymbolParams.model_json_schema(),
    },
    {
        "name": "detect_patterns",
        "description": (
            "Scan files for architectural patterns: webhook/callback endpoints, "
            "queue consumer/producer, retry/backoff logic, lock/mutex usage, "
            "check-then-act anti-patterns, transaction boundaries, token lifecycle, "
            "and side-effect chains. Returns structured matches with file, line, "
            "pattern category, and a snippet. Use this to quickly identify risky "
            "code patterns before diving into detailed review."
        ),
        "input_schema": DetectPatternsParams.model_json_schema(),
    },
    {
        "name": "run_test",
        "description": (
            "Run a specific test file or test function and return the result. "
            "Use this as a VERIFICATION step to prove a bug exists — e.g. run "
            "the test that covers a changed function to see if it fails. "
            "Returns pass/fail status, output, and failure details. "
            "Only use after you have identified a likely finding and want to "
            "confirm it with evidence from actual test execution."
        ),
        "input_schema": RunTestParams.model_json_schema(),
    },
    # --- New analysis tools ---
    {
        "name": "git_hotspots",
        "description": (
            "Analyze git history to find frequently changed files (hotspots) "
            "and recently active areas. Hotspots indicate code that changes often — "
            "likely complex, risky, or under active development. Use to prioritize "
            "investigation in large codebases."
        ),
        "input_schema": GitHotspotsParams.model_json_schema(),
    },
    {
        "name": "list_endpoints",
        "description": (
            "Extract all API endpoints/routes from the codebase. Detects patterns "
            "for FastAPI, Flask, Django, Spring, Express, and Go. Returns method, "
            "path, file, and line for each endpoint. Use as a starting point when "
            "investigating API flows or understanding the service surface."
        ),
        "input_schema": ListEndpointsParams.model_json_schema(),
    },
    {
        "name": "extract_docstrings",
        "description": (
            "Extract function/class-level documentation (docstrings, JSDoc, Javadoc, "
            "Go doc comments) from a file. Use when compressed_view isn't enough and "
            "you need to understand what a function is supposed to do without reading "
            "its full implementation."
        ),
        "input_schema": ExtractDocstringsParams.model_json_schema(),
    },
    {
        "name": "db_schema",
        "description": (
            "Extract database schema from ORM models (SQLAlchemy, Django, JPA, TypeORM). "
            "Returns model names, table names, and field definitions. Use to understand "
            "the data layer — what tables exist, what columns they have, and how models "
            "relate to each other."
        ),
        "input_schema": DbSchemaParams.model_json_schema(),
    },
    # --- Browser tools ---
    {
        "name": "web_search",
        "description": (
            "Search the web using Google and return structured results. "
            "Each result includes title, URL, and snippet. Use this to look up "
            "external library documentation, error messages, API references, "
            "or best practices. Follow up with web_navigate on interesting URLs."
        ),
        "input_schema": WebSearchParams.model_json_schema(),
    },
    {
        "name": "web_navigate",
        "description": (
            "Navigate a headless browser to a URL and return the page content. "
            "Returns the page title, final URL (after redirects), visible text, "
            "and a list of links. The browser session persists across calls so "
            "you can navigate, click, fill forms, and extract data in sequence."
        ),
        "input_schema": WebNavigateParams.model_json_schema(),
    },
    {
        "name": "web_click",
        "description": (
            "Click an element on the current browser page by CSS selector or "
            "visible text. Returns the page state after the click (URL, title, "
            "and nearby text). Use after web_navigate to interact with buttons, "
            "links, tabs, and other clickable elements."
        ),
        "input_schema": WebClickParams.model_json_schema(),
    },
    {
        "name": "web_fill",
        "description": (
            "Fill a form input on the current browser page. Clears the field "
            "first, then types the value. Optionally press Enter after filling "
            "(useful for search boxes). Use after web_navigate to submit forms."
        ),
        "input_schema": WebFillParams.model_json_schema(),
    },
    {
        "name": "web_screenshot",
        "description": (
            "Take a screenshot of the current browser page (or a specific element). "
            "Returns the path to the saved PNG file. Use to capture visual state "
            "when text extraction is insufficient."
        ),
        "input_schema": WebScreenshotParams.model_json_schema(),
    },
    {
        "name": "web_extract",
        "description": (
            "Extract text or attributes from elements matching a CSS selector "
            "on the current browser page. Use to scrape structured data like "
            "tables, lists, or specific sections. Returns an array of matches."
        ),
        "input_schema": WebExtractParams.model_json_schema(),
    },
    # --- Interactive tool (only available in interactive mode) ---
    {
        "name": "ask_user",
        "description": (
            "Ask the user for direction when there are multiple valid approaches "
            "and their preference would materially change your investigation. "
            "Call this in your first iteration, before exploring the codebase. "
            "Provide 2-4 concrete options when possible, and mark your "
            "recommended option. The user's answer is returned as the tool "
            "result. Use at most once per session."
        ),
        "input_schema": AskUserParams.model_json_schema(),
    },
    # --- Signal blocker (only available for Brain-dispatched sub-agents) ---
    {
        "name": "signal_blocker",
        "description": (
            "Ask the Brain orchestrator for direction when you encounter "
            "ambiguity that you cannot resolve from the codebase alone. "
            "Provide 2-4 concrete options. The Brain will respond with "
            "a direction to follow. Use sparingly — only when genuinely stuck."
        ),
        "input_schema": SignalBlockerParams.model_json_schema(),
    },
]


# ---------------------------------------------------------------------------
# Brain orchestrator tool definitions (separate from TOOL_DEFINITIONS)
#
# These are meta-tools for the Brain agent only — they dispatch sub-agents
# and evaluate findings. Never exposed to regular explorer/review agents,
# never included in parity tests, never proxied to the VS Code extension.
# ---------------------------------------------------------------------------

BRAIN_TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "name": "dispatch_agent",
        "description": (
            "Dispatch a specialist agent to investigate a specific aspect of "
            "the codebase. The agent runs in an isolated context with its own "
            "tools and budget, then returns condensed findings (answer, file "
            "references, gaps identified). Choose the agent based on the "
            "available agents list in your system prompt."
        ),
        "input_schema": DispatchAgentParams.model_json_schema(),
    },
    {
        "name": "dispatch_swarm",
        "description": (
            "Dispatch a predefined group of parallel agents. Only use for "
            "tasks that require multiple perspectives simultaneously: "
            "'pr_review' (5-agent code review) or 'business_flow' (2-agent "
            "flow tracing). For all other tasks, use dispatch_agent instead. "
            "The result includes a synthesis_guide with instructions for "
            "how to combine the agents' findings."
        ),
        "input_schema": DispatchSwarmParams.model_json_schema(),
    },
]


SIGNAL_BLOCKER_TOOL_DEF: Dict[str, Any] = {
    "name": "signal_blocker",
    "description": (
        "Ask the Brain orchestrator for direction when you encounter "
        "ambiguity that you cannot resolve from the codebase alone. "
        "Provide 2-4 concrete options. The Brain will respond with "
        "a direction to follow. Use sparingly — only when genuinely stuck."
    ),
    "input_schema": SignalBlockerParams.model_json_schema(),
}


def get_brain_tool_definitions() -> List[Dict[str, Any]]:
    """Return Brain tool definitions + ask_user for Brain's tool list."""
    ask_user_def = next(t for t in TOOL_DEFINITIONS if t["name"] == "ask_user")
    return BRAIN_TOOL_DEFINITIONS + [ask_user_def]
