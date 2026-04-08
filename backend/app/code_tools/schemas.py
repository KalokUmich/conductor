"""Pydantic schemas for code intelligence tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------------------
# Tool parameter schemas
# ---------------------------------------------------------------------------


class GrepParams(BaseModel):
    pattern: str = Field(
        ..., description="Python regex pattern. Use | for alternation (NOT \\|). Example: 'Foo|Bar' matches Foo or Bar."
    )
    path: Optional[str] = Field(None, description="Relative path within workspace to search (file or directory).")
    include_glob: Optional[str] = Field(
        None, description="Glob to filter files by extension, e.g. '*.java', '*.py'. Omit to search all files."
    )
    max_results: int = Field(default=50, ge=1, le=200)
    output_mode: str = Field(
        default="content",
        description="Output format: 'content' (matching lines, default), 'files_only' (just file paths), 'count' (match count per file).",
    )
    context_lines: int = Field(
        default=0,
        ge=0,
        le=10,
        description="Lines of context to show before and after each match (0 = match line only).",
    )
    case_insensitive: bool = Field(default=False, description="Case-insensitive matching.")
    multiline: bool = Field(default=False, description="Match across line boundaries (re.DOTALL).")
    file_type: Optional[str] = Field(
        None,
        description="Language shortcut for file filtering: py, js, ts, java, go, rust, c, cpp. Maps to include_glob automatically.",
    )


class ReadFileParams(BaseModel):
    path: str = Field(..., description="Relative file path within workspace.")
    start_line: Optional[int] = Field(None, ge=1, description="First line to read (1-based).")
    end_line: Optional[int] = Field(None, ge=1, description="Last line to read (1-based, inclusive).")


class ListFilesParams(BaseModel):
    directory: str = Field(default=".", description="Relative directory within workspace.")
    max_depth: Optional[int] = Field(default=3, ge=1, le=10)
    include_glob: Optional[str] = Field(None, description="Glob to filter, e.g. '*.py'.")


class GlobParams(BaseModel):
    pattern: str = Field(
        ..., description="Glob pattern to match files (e.g. '**/*.py', 'src/**/*.ts', '**/test_*.py')."
    )
    path: Optional[str] = Field(
        None, description="Directory to search in (relative to workspace). Defaults to workspace root."
    )


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
        default=10,
        ge=0,
        le=50,
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
    language: Optional[str] = Field(
        None, description="Language hint: python, javascript, typescript, go, rust, java, c, cpp."
    )
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
    symbol_name: str = Field(
        ..., description="Name of the symbol to expand (e.g. 'PaymentService' or 'process_payment')."
    )
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
        default=30,
        ge=5,
        le=60,
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
    question: str = Field(
        ..., description="The clarifying question to ask the user. Be specific about what information you need."
    )
    options: List[str] = Field(
        default_factory=list,
        description="2-4 concrete options for the user to choose from. Each option is a short label (e.g. 'Focus on authentication flow'). The user can also type a free-form answer instead of picking an option.",
    )
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


class CreatePlanParams(BaseModel):
    mode: str = Field(..., description="Dispatch mode: 'simple', 'complex', 'swarm', or 'transfer'")
    reasoning: str = Field(..., description="Why this mode and agent(s) — what about the query led to this decision")
    agents: List[str] = Field(default_factory=list, description="Agent(s) to dispatch, in order")
    query_decomposition: List[str] = Field(
        default_factory=list, description="For swarm/complex: how the query breaks into sub-questions"
    )
    risk: str = Field(default="", description="Key risks or ambiguities in this investigation")
    fallback: str = Field(default="", description="What to try if the primary approach finds insufficient evidence")


class DispatchAgentParams(BaseModel):
    query: str = Field(..., description="Focused question for the agent to investigate")

    # Mode 1: Template (pre-defined agent from registry)
    template: Optional[str] = Field(
        default=None,
        description="Pre-defined agent template name (e.g. 'correctness', "
        "'explore_implementation'). Use for PR review and business flow swarm agents.",
    )

    # Mode 2: Dynamic composition (Brain assembles the agent)
    tools: Optional[List[str]] = Field(
        default=None,
        description="Tools for this agent (e.g. ['grep', 'read_file', "
        "'find_symbol']). Required when no template is specified.",
    )
    perspective: Optional[str] = Field(
        default=None, description="1-3 sentences defining the agent's investigation focus and what to look for."
    )
    skill: Optional[str] = Field(
        default=None,
        description="Investigation skill key from the skill catalog "
        "(e.g. 'entry_point', 'root_cause', 'architecture', 'impact', "
        "'data_lineage', 'recent_changes', 'code_explanation', "
        "'config_analysis', 'issue_tracking').",
    )
    model: str = Field(
        default="explorer",
        description="'explorer' (Haiku, default) or 'strong' (Sonnet, for complex reasoning like root cause analysis).",
    )
    budget_tokens: Optional[int] = Field(
        default=None, ge=50000, le=800000, description="Token budget override. Defaults based on skill type."
    )
    max_iterations: Optional[int] = Field(
        default=None, ge=5, le=30, description="Iteration limit override. Default: 20."
    )

    # Shared
    budget_weight: float = Field(default=1.0, ge=0.3, le=2.0, description="Budget multiplier (1.0 = standard)")


class DispatchSwarmParams(BaseModel):
    swarm_name: str = Field(
        ..., description="Swarm preset name (e.g. 'pr_review', 'business_flow'). Only use predefined swarms."
    )
    query: str = Field(..., description="Shared investigation query for all agents in the swarm")


class TransferToBrainParams(BaseModel):
    brain_name: str = Field(..., description="Target specialized brain (e.g. 'pr_review')")
    workspace_path: str = Field(..., description="Workspace path for the review")
    diff_spec: str = Field(default="", description="Git diff spec (e.g. 'main...feature/branch', 'HEAD~1..HEAD')")


# ---------------------------------------------------------------------------
# Browser tool parameter schemas
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Jira integration tool parameter schemas
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# File editing tool parameter schemas
# ---------------------------------------------------------------------------


class FileEditParams(BaseModel):
    path: str = Field(..., description="Relative path to the file within the workspace.")
    old_string: str = Field(
        ...,
        description="Exact string to find in the file. Must match the file content precisely (including whitespace/indentation). Use read_file first to see the exact content.",
    )
    new_string: str = Field(..., description="Replacement string. Can be empty to delete the matched text.")
    replace_all: bool = Field(
        default=False,
        description="If true, replace ALL occurrences. If false (default), the old_string must be unique in the file.",
    )


class FileWriteParams(BaseModel):
    path: str = Field(..., description="Relative path for the file. Parent directories are created automatically.")
    content: str = Field(
        ...,
        description="Complete file content to write. For existing files, this overwrites the entire content — use file_edit for partial changes.",
    )


class JiraSearchParams(BaseModel):
    query: str = Field(
        ...,
        description="JQL query or natural language search text (e.g. 'project = DEV AND status = \"In Progress\"', 'auth refactor').",
    )
    max_results: int = Field(default=10, ge=1, le=50, description="Max issues to return.")


class JiraGetIssueParams(BaseModel):
    issue_key: str = Field(..., description="Jira issue key (e.g. 'DEV-123', 'HELP-42').")

    @model_validator(mode="before")
    @classmethod
    def _normalize_key(cls, data: Any) -> Any:
        """Accept 'key' as alias for 'issue_key' (LLMs sometimes shorten param names)."""
        if isinstance(data, dict) and "key" in data and "issue_key" not in data:
            data["issue_key"] = data.pop("key")
        return data


class JiraCreateIssueParams(BaseModel):
    project_key: str = Field(
        ..., description="Jira project key (e.g. 'DEV', 'HELP'). Use jira_list_projects to discover available projects."
    )
    summary: str = Field(..., description="Issue title / summary.")
    description: str = Field(
        default="",
        description="Issue description with context. Include affected files, code snippets, and steps to reproduce where relevant.",
    )
    issue_type: str = Field(
        default="Software Task",
        description="Issue type: 'Software Task' (small work), 'Bug' (defect fix), 'Epic' (medium project, will contain sub-tasks), 'Project' (large initiative).",
    )
    priority: str = Field(
        default="", description="Priority: Highest, High, Medium, Low, Lowest. Empty = project default."
    )
    components: List[str] = Field(
        default_factory=list,
        description="Component names (e.g. ['JBE', 'Render API']). Use jira_list_projects to see available components.",
    )
    team: str = Field(default="", description="Team name (e.g. 'Platform', 'FinOps'). Empty = unassigned.")
    parent_key: str = Field(
        default="",
        description="Parent issue key for sub-tasks under an Epic (e.g. 'DEV-100'). Required when creating child tickets of an Epic.",
    )


class JiraUpdateIssueParams(BaseModel):
    issue_key: str = Field(..., description="Jira issue key (e.g. 'DEV-123').")
    transition_to: str = Field(
        default="",
        description="Target status name to transition to (e.g. 'To Do', 'In Progress'). Agent CANNOT set Done/Closed/Resolved — those require manual user action. Use empty string to skip.",
    )
    comment: str = Field(default="", description="Comment to add to the issue. Include code references and findings.")
    description_append: str = Field(
        default="",
        description="Text to APPEND to the existing description. A Conductor separator is automatically inserted. Use for adding analysis findings, affected files, and change summaries back to the ticket. Never overwrites existing content.",
    )
    priority: str = Field(
        default="", description="New priority: Highest, High, Medium, Low, Lowest. Empty = no change."
    )
    labels_add: List[str] = Field(default_factory=list, description="Labels to add (e.g. ['needs-review', 'backend']).")

    @model_validator(mode="before")
    @classmethod
    def _normalize_key(cls, data: Any) -> Any:
        """Accept 'key' as alias for 'issue_key' (LLMs sometimes shorten param names)."""
        if isinstance(data, dict) and "key" in data and "issue_key" not in data:
            data["issue_key"] = data.pop("key")
        return data


class JiraListProjectsParams(BaseModel):
    pass  # No params — returns all allowed projects with metadata


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
    "glob": GlobParams,
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
    # File editing tools
    "file_edit": FileEditParams,
    "file_write": FileWriteParams,
    # Jira integration tools
    "jira_search": JiraSearchParams,
    "jira_get_issue": JiraGetIssueParams,
    "jira_create_issue": JiraCreateIssueParams,
    "jira_list_projects": JiraListProjectsParams,
    "jira_update_issue": JiraUpdateIssueParams,
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
    # ------------------------------------------------------------------
    # Code Search & Navigation
    # ------------------------------------------------------------------
    {
        "name": "grep",
        "description": (
            "Search for a regex pattern across files in the workspace.\n\n"
            "Usage:\n"
            "- Use grep for text/pattern searches across multiple files.\n"
            "- Use path to scope search to a subdirectory — dramatically reduces time and noise.\n"
            "- Use find_symbol instead when you need a definition (class, function, method).\n"
            "- Use find_references instead when you need all usages of a specific symbol "
            "(it filters out comments, strings, and partial matches).\n"
            "- Use ast_search instead for structural code patterns (e.g. all if-else blocks, "
            "all method calls on a specific object).\n\n"
            "Pattern tips (Python regex syntax):\n"
            "- Class names: 'class\\s+Approval'\n"
            "- Method calls: 'approve\\('\n"
            "- Multiple terms: 'APPROVED|REJECTED|PENDING'\n"
            "- Literal pipe: '\\|' (escape with backslash)\n\n"
            "If you get 0 results, try a simpler substring pattern (drop regex metacharacters) "
            "or use find_symbol instead.\n"
            "If you get 40+ results, narrow with path or include_glob.\n"
            "Only use include_glob if you know the exact file extension (e.g. '*.java', '*.py'). "
            "Omit include_glob to search all file types."
        ),
        "input_schema": GrepParams.model_json_schema(),
    },
    {
        "name": "read_file",
        "description": (
            "Read file contents with optional line range.\n\n"
            "Usage:\n"
            "- Prefer calling file_outline or compressed_view first on files over 100 lines "
            "— saves tokens by revealing method locations so you can read_file with a targeted range.\n"
            "- Avoid reading 300+ line files in full — use start_line/end_line to save tokens.\n"
            "- Use file_outline instead if you only need the list of classes/functions in a file.\n"
            "- Use compressed_view instead if you need signatures + call relationships + side effects.\n"
            "- Use expand_symbol instead if you need one specific function's source code.\n\n"
            "Returns the file text, total line count, and file path. "
            "Does not return AST structure or symbol definitions — use file_outline for that."
        ),
        "input_schema": ReadFileParams.model_json_schema(),
    },
    {
        "name": "list_files",
        "description": (
            "List files and directories under a path, with recursive depth control.\n\n"
            "Usage:\n"
            "- Use this to understand project layout before diving into specific files — "
            "e.g. list_files('src/services') reveals all service files.\n"
            "- Use include_glob to filter by extension (e.g. '*.java').\n"
            "- Use module_summary instead when you need to understand what a directory contains "
            "(classes, functions, imports, dependencies) — not just file names.\n\n"
            "Returns paths only, not file contents."
        ),
        "input_schema": ListFilesParams.model_json_schema(),
    },
    {
        "name": "glob",
        "description": (
            "Fast file pattern matching — returns file paths sorted by modification time "
            "(most recently modified first).\n\n"
            "Usage:\n"
            "- Use when you know the filename pattern but not the exact directory "
            "(e.g. glob('**/test_*.py') finds all test files).\n"
            "- Supports glob patterns: '**/*.py', 'src/**/*.ts', '**/Dockerfile'.\n"
            "- '**' must be a COMPLETE path segment. '**/*.py' is valid; '**.py' "
            "and 'src**/foo' are NOT.\n"
            "- Use list_files instead to browse a directory tree with depth control.\n"
            "- Use grep instead to search file contents (not file names).\n"
            "- Use find_symbol instead to find where a class/function is defined."
        ),
        "input_schema": GlobParams.model_json_schema(),
    },
    {
        "name": "find_symbol",
        "description": (
            "Find symbol definitions (functions, classes, methods, interfaces) by name "
            "using AST parsing. Returns exact file locations with line numbers and signatures.\n\n"
            "Usage:\n"
            "- Prefer find_symbol when you need to locate where a class or function "
            "is defined — it is exact and filters out usages/imports.\n"
            "- Use grep instead for text patterns, error messages, config keys, or string literals.\n"
            "- Use find_references instead when you need all places a symbol is used (not where it's defined).\n\n"
            "Example: find_symbol('ApplicationDecisionService') finds the class definition; "
            "grep('ApplicationDecisionService') finds every mention including imports.\n\n"
            "If you get 0 results, the name may be misspelled or use a different convention. "
            "Try grep with a partial name to discover the correct spelling."
        ),
        "input_schema": FindSymbolParams.model_json_schema(),
    },
    {
        "name": "find_references",
        "description": (
            "Find all references (usages) of a symbol across the codebase. "
            "Combines grep with AST validation — filters out comments, strings, "
            "and partial matches for accurate results.\n\n"
            "Usage:\n"
            "- Use this when you need to know every place a class, function, or constant is used.\n"
            "- Different from find_symbol (which finds definitions only).\n"
            "- Different from get_dependents (which works at file/module level, not symbol level).\n"
            "- Use the file parameter to scope results when the symbol has many references.\n\n"
            "Example: find_references('affordability_score') shows every file that reads or writes it."
        ),
        "input_schema": FindReferencesParams.model_json_schema(),
    },
    {
        "name": "file_outline",
        "description": (
            "Get the structure of a file: all classes, functions, methods with line numbers "
            "and signatures.\n\n"
            "Usage:\n"
            "- Prefer calling this before read_file on files >100 lines "
            "— reveals method names and line ranges so you can target specific sections.\n"
            "- Use compressed_view instead if you also need call relationships, side effects, "
            "and exceptions (richer but slightly more output).\n"
            "- Use module_summary instead if you need an overview of an entire directory.\n\n"
            "Useful for answering 'what methods does this class have?' in one call."
        ),
        "input_schema": FileOutlineParams.model_json_schema(),
    },
    # ------------------------------------------------------------------
    # Dependency Analysis
    # ------------------------------------------------------------------
    {
        "name": "get_dependencies",
        "description": (
            "Find what files a given file imports (downstream dependencies). "
            "Uses the static dependency graph built from import statements.\n\n"
            "Usage:\n"
            "- Use this to answer 'what does this file rely on?' — e.g. "
            "get_dependencies('PaymentService.java') shows all models, clients, and utilities it imports.\n"
            "- Set max_depth=2 or 3 to find transitive dependencies (A imports B imports C).\n"
            "- Use get_dependents (reverse) to find what depends ON this file.\n"
            "- Does NOT find runtime dependencies or reflection-based usage — use grep for those."
        ),
        "input_schema": GetDependenciesParams.model_json_schema(),
    },
    {
        "name": "get_dependents",
        "description": (
            "Find what files depend on (import) a given file — the reverse of get_dependencies.\n\n"
            "Usage:\n"
            "- Use this for blast radius analysis: 'if I change this file, what else is affected?'\n"
            "- Set max_depth=2 or 3 for transitive dependents.\n"
            "- Different from find_references: get_dependents works at file/module level (import graph), "
            "while find_references finds individual symbol usages.\n"
            "- Use get_dependents for broad file-level impact; find_references for specific symbol tracking."
        ),
        "input_schema": GetDependentsParams.model_json_schema(),
    },
    # ------------------------------------------------------------------
    # Git & Version Control
    # ------------------------------------------------------------------
    {
        "name": "git_log",
        "description": (
            "Show recent git commits with hash, author, date, and message.\n\n"
            "Usage:\n"
            "- Use file= to filter commits touching a specific file.\n"
            "- Use search= to find commits mentioning specific terms (e.g. 'CVE', 'timeout', 'fix').\n"
            "- Returns commit metadata only — does NOT include diffs.\n"
            "- Follow up with git_show on specific commits to see the actual changes.\n"
            "- Use git_hotspots instead if you want to find frequently-changed files."
        ),
        "input_schema": GitLogParams.model_json_schema(),
    },
    {
        "name": "git_diff",
        "description": (
            "Show the full unified diff between two git refs (commits, branches).\n\n"
            "Usage:\n"
            "- ALWAYS use file= to limit to a single file — reviewing one file at a time "
            "prevents context overflow.\n"
            "- For large PRs, use git_diff_files FIRST to get the file list, then git_diff "
            "with file= for each file you want to review.\n"
            "- Do NOT diff entire large PRs without file= — the output will be truncated.\n"
            "- Use context_lines to control surrounding context (default 10).\n"
            "- Returns raw diff text, not parsed structures."
        ),
        "input_schema": GitDiffParams.model_json_schema(),
    },
    {
        "name": "git_diff_files",
        "description": (
            "List files changed between two git refs with status and line counts.\n\n"
            "Usage:\n"
            "- ALWAYS use this FIRST in code review to get an overview before reading diffs.\n"
            "- Supports three-dot syntax for PR diffs: 'master...feature/xxx'.\n"
            "- Returns: path, status (added/modified/deleted/renamed), additions, deletions.\n"
            "- Follow up with git_diff with file= to review individual files."
        ),
        "input_schema": GitDiffFilesParams.model_json_schema(),
    },
    {
        "name": "git_blame",
        "description": (
            "Run git blame on a file to see who last changed each line, with commit hash, "
            "author, and date.\n\n"
            "Usage:\n"
            "- Use start_line/end_line to limit to a specific region.\n"
            "- Use this to trace when and by whom specific code was introduced.\n"
            "- Follow up with git_show on interesting commit hashes to understand WHY the change was made."
        ),
        "input_schema": GitBlameParams.model_json_schema(),
    },
    {
        "name": "git_show",
        "description": (
            "Show full details of a specific git commit: author, date, full commit message "
            "(including body/PR description), and the diff.\n\n"
            "Usage:\n"
            "- Use after git_log or git_blame to understand the motivation behind a change.\n"
            "- Use file= to limit the diff to a single file when the commit touches many files.\n"
            "- For commit metadata only (no diff), use git_log instead."
        ),
        "input_schema": GitShowParams.model_json_schema(),
    },
    {
        "name": "git_hotspots",
        "description": (
            "Find frequently changed files (hotspots) and recently active areas in git history.\n\n"
            "Usage:\n"
            "- Hotspots indicate code that changes often — likely complex, risky, or under active development.\n"
            "- Use to prioritize investigation in large codebases.\n"
            "- Use days= to control the lookback window (default 90 days).\n"
            "- Use git_log instead if you need specific commit details, not aggregate frequency."
        ),
        "input_schema": GitHotspotsParams.model_json_schema(),
    },
    # ------------------------------------------------------------------
    # Code Analysis & Tracing
    # ------------------------------------------------------------------
    {
        "name": "ast_search",
        "description": (
            "Structural AST search using ast-grep patterns — matches code structure, not text.\n\n"
            "Usage:\n"
            "- Use for structural patterns that grep cannot express reliably: catches all variations "
            "regardless of whitespace, comments, or formatting.\n"
            "- Use grep instead for simple text/name searches — ast_search is for structural patterns only.\n"
            "- Use $VAR for single nodes, $$$VAR for multiple nodes.\n\n"
            "Examples:\n"
            "- Function definitions: 'def $F($$$ARGS)'\n"
            "- Conditionals: 'if $COND: $$$BODY'\n"
            "- Method calls: '$OBJ.$METHOD($$$ARGS)'\n"
            "- Try-except: 'try: $$$BODY except $EXC: $$$HANDLER'\n\n"
            "Requires ast-grep-cli. If ast_search fails or returns unexpected results, "
            "fall back to grep with a regex pattern."
        ),
        "input_schema": AstSearchParams.model_json_schema(),
    },
    {
        "name": "get_callees",
        "description": (
            "Find all functions/methods called within a specific function body.\n\n"
            "Usage:\n"
            "- ESSENTIAL for tracing business flows: after finding an entry point, call get_callees "
            "to discover ALL downstream services it invokes (e.g. email, payment, verification).\n"
            "- Requires both function_name and file path.\n"
            "- Use get_callers (reverse) to find who calls a given function.\n"
            "- Use trace_variable instead if you need to track a specific value through the call chain.\n\n"
            "Reveals the complete chain of steps without reading the entire file."
        ),
        "input_schema": GetCalleesParams.model_json_schema(),
    },
    {
        "name": "get_callers",
        "description": (
            "Find all functions/methods that call a given function across the codebase.\n\n"
            "Usage:\n"
            "- Essential for impact analysis: 'if I change this function, who is affected?'\n"
            "- Use path= to scope the search to a subdirectory.\n"
            "- Use get_callees (reverse) to find what a function calls downstream.\n"
            "- Different from find_references: get_callers finds the enclosing function that makes "
            "the call; find_references finds every mention including imports and type annotations.\n\n"
            "Example: get_callers('make_decision') reveals every path that triggers a lending decision."
        ),
        "input_schema": GetCallersParams.model_json_schema(),
    },
    {
        "name": "trace_variable",
        "description": (
            "Trace a variable's data flow through function calls.\n\n"
            "Forward: finds where the value goes — aliases, argument-to-parameter mapping, "
            "and sinks (ORM filters, SQL parameters, HTTP bodies, return statements).\n"
            "Backward: finds where the value comes from — callers that pass this parameter, "
            "and sources (HTTP requests, config, DB results).\n\n"
            "Usage:\n"
            "- Use to answer 'how does loan_id flow from the HTTP request into the SQL WHERE clause?'\n"
            "- Chain forward hops: the flows_to output of one call becomes the input of the next.\n"
            "- Use get_callees/get_callers instead for function-level call chains (not value-level).\n\n"
            "Example: trace_variable('loan_id', 'services/lending.py', direction='forward')"
        ),
        "input_schema": TraceVariableParams.model_json_schema(),
    },
    # ------------------------------------------------------------------
    # Testing
    # ------------------------------------------------------------------
    {
        "name": "find_tests",
        "description": (
            "Find test functions that test a given function or class.\n\n"
            "Usage:\n"
            "- Use to check if a function has tests, or to find test examples that document expected behavior.\n"
            "- Searches test files (test_*.py, *_test.py, *.test.ts, *.spec.ts, *_test.go, *Test.java, *_test.rs).\n"
            "- Returns test file paths and function names.\n"
            "- Follow up with test_outline for details about what each test mocks and asserts.\n"
            "- Follow up with run_test to execute and verify."
        ),
        "input_schema": FindTestsParams.model_json_schema(),
    },
    {
        "name": "test_outline",
        "description": (
            "Get the detailed structure of a test file: test classes, test functions, mocks, "
            "assertions, and fixtures.\n\n"
            "Usage:\n"
            "- Use this to understand what a test file covers without reading every line.\n"
            "- Richer than file_outline — understands test semantics (patch/MagicMock/jest.fn/vi.mock, "
            "assert patterns, fixtures) for pytest, jest, mocha, vitest, and Go.\n"
            "- Use file_outline instead for non-test files.\n"
            "- Does NOT execute tests — use run_test for that."
        ),
        "input_schema": TestOutlineParams.model_json_schema(),
    },
    {
        "name": "run_test",
        "description": (
            "Run a specific test file or test function and return the result.\n\n"
            "Usage:\n"
            "- Use as a VERIFICATION step to prove a bug exists or confirm a fix works.\n"
            "- Only use AFTER you have identified a likely finding and want to confirm it "
            "with evidence from actual test execution.\n"
            "- Returns pass/fail status, output, and failure details.\n"
            "- Use find_tests first to discover which tests cover the code in question."
        ),
        "input_schema": RunTestParams.model_json_schema(),
    },
    # ------------------------------------------------------------------
    # Compression & Summary (token-efficient exploration)
    # ------------------------------------------------------------------
    {
        "name": "compressed_view",
        "description": (
            "Compressed view of a file: function/class signatures, call relationships, "
            "side effects (DB writes, HTTP calls, file I/O), and exceptions raised. "
            "Saves ~80%% tokens vs read_file.\n\n"
            "Usage:\n"
            "- Use this as the DEFAULT first step to understand any file — before read_file.\n"
            "- Use the focus parameter to filter to a specific class or function.\n"
            "- Use read_file with start_line/end_line or expand_symbol when you need the actual "
            "implementation (function bodies, logic).\n"
            "- Use file_outline instead if you only need names and line numbers (lighter output).\n"
            "- Use module_summary instead for a directory-level overview."
        ),
        "input_schema": CompressedViewParams.model_json_schema(),
    },
    {
        "name": "module_summary",
        "description": (
            "High-level summary of a module/directory: classes, functions, imports, "
            "dependencies, and file list. Saves ~95%% tokens vs reading all files.\n\n"
            "Usage:\n"
            "- Use this as your FIRST step when exploring an unfamiliar directory.\n"
            "- Reveals the major components and their relationships so you can target specific files.\n"
            "- Follow up with compressed_view on specific files of interest.\n"
            "- Use list_files instead if you only need file names without code structure.\n"
            "- Does NOT show function bodies, line-level detail, or test coverage."
        ),
        "input_schema": ModuleSummaryParams.model_json_schema(),
    },
    {
        "name": "expand_symbol",
        "description": (
            "Expand a symbol to its full source code with line numbers.\n\n"
            "Usage:\n"
            "- Use after compressed_view or file_outline when you need the complete "
            "implementation of a specific function or class.\n"
            "- Avoids reading the entire file — returns only the symbol body.\n"
            "- Provide file_path for faster lookup, or omit to search the workspace.\n"
            "- Use read_file with start_line/end_line instead when you need surrounding "
            "context (nearby comments, adjacent methods, class-level fields)."
        ),
        "input_schema": ExpandSymbolParams.model_json_schema(),
    },
    # ------------------------------------------------------------------
    # Pattern Detection & Extraction
    # ------------------------------------------------------------------
    {
        "name": "detect_patterns",
        "description": (
            "Scan files for architectural patterns: webhook/callback endpoints, "
            "queue consumer/producer, retry/backoff logic, lock/mutex usage, "
            "check-then-act anti-patterns, transaction boundaries, token lifecycle, "
            "and side-effect chains.\n\n"
            "Usage:\n"
            "- Use to quickly identify risky code patterns before diving into detailed review.\n"
            "- Use the categories parameter to focus on specific patterns "
            "(e.g. categories=['retry','transaction']) rather than scanning everything.\n"
            "- Returns structured matches with file, line, pattern category, and snippet.\n"
            "- Does NOT verify correctness — it finds pattern instances that warrant deeper investigation.\n"
            "- Follow up with read_file or compressed_view on flagged files to verify."
        ),
        "input_schema": DetectPatternsParams.model_json_schema(),
    },
    {
        "name": "list_endpoints",
        "description": (
            "Extract all API endpoints/routes from the codebase.\n\n"
            "Usage:\n"
            "- Use as a starting point when investigating API flows or understanding the service surface.\n"
            "- Detects patterns for FastAPI, Flask, Django, Spring, Express, and Go.\n"
            "- Returns method, path, file, and line for each endpoint.\n"
            "- Follow up with get_callees on the handler function to trace the request flow.\n"
            "- Use grep instead if you need to search for a specific endpoint path."
        ),
        "input_schema": ListEndpointsParams.model_json_schema(),
    },
    {
        "name": "extract_docstrings",
        "description": (
            "Extract function/class-level documentation (docstrings, JSDoc, Javadoc, "
            "Go doc comments) from a file.\n\n"
            "Usage:\n"
            "- Use when you need to understand what a function is supposed to do without reading "
            "its full implementation.\n"
            "- Use compressed_view instead if you need signatures + side effects + call relationships.\n"
            "- Use symbol_name parameter to extract docs for a specific symbol only."
        ),
        "input_schema": ExtractDocstringsParams.model_json_schema(),
    },
    {
        "name": "db_schema",
        "description": (
            "Extract database schema from ORM models (SQLAlchemy, Django, JPA, TypeORM).\n\n"
            "Usage:\n"
            "- Use to understand the data layer: what tables exist, what columns they have, "
            "and how models relate to each other.\n"
            "- Returns model names, table names, and field definitions.\n"
            "- Use grep instead to find specific column names or table references in code.\n"
            "- Use trace_variable to trace how data flows from models into queries."
        ),
        "input_schema": DbSchemaParams.model_json_schema(),
    },
    # ------------------------------------------------------------------
    # File editing tools
    # ------------------------------------------------------------------
    {
        "name": "file_edit",
        "description": (
            "Edit an existing file by replacing an exact string match.\n\n"
            "Usage:\n"
            "- You MUST use read_file on the file first — editing a file you haven't read will fail.\n"
            "- old_string must match the file content exactly, including whitespace and indentation.\n"
            "- If old_string appears multiple times, set replace_all=true or provide more context to make it unique.\n"
            "- Returns a unified diff of the changes for user review.\n"
            "- Cannot edit files in .git/, node_modules/, or .env files."
        ),
        "input_schema": FileEditParams.model_json_schema(),
    },
    {
        "name": "file_write",
        "description": (
            "Create a new file or completely overwrite an existing file.\n\n"
            "Usage:\n"
            "- For new files: provide the full content. Parent directories are created automatically.\n"
            "- For existing files: you MUST read_file first. This overwrites the entire file.\n"
            "- Prefer file_edit for partial changes to existing files — it's safer and generates cleaner diffs.\n"
            "- Cannot write to .git/, node_modules/, or .env files."
        ),
        "input_schema": FileWriteParams.model_json_schema(),
    },
    # ------------------------------------------------------------------
    # Jira integration tools
    # ------------------------------------------------------------------
    {
        "name": "jira_search",
        "description": (
            "Search Jira issues using JQL or free text.\n\n"
            "Usage:\n"
            "- Use JQL for structured queries: 'project = DEV AND status = \"In Progress\"'\n"
            "- Use free text for keyword search: 'auth refactor'\n"
            "- Convenience shortcuts: 'my tickets', 'my sprint', 'blockers' — auto-expands to JQL.\n"
            "- Returns issue key, summary, status, priority, assignee, and browse URL.\n"
            "- Use to check for duplicate tickets before creating, or to find related work."
        ),
        "input_schema": JiraSearchParams.model_json_schema(),
    },
    {
        "name": "jira_get_issue",
        "description": (
            "Get full details of a Jira issue by key.\n\n"
            "Usage:\n"
            "- Returns description, status, priority, assignee, comments, and subtasks.\n"
            "- Use to understand what a ticket requires before suggesting code changes.\n"
            "- Use jira_search first if you don't have the exact issue key."
        ),
        "input_schema": JiraGetIssueParams.model_json_schema(),
    },
    {
        "name": "jira_create_issue",
        "description": (
            "Create a new Jira ticket. Requires ask_user confirmation before calling.\n\n"
            "Usage:\n"
            "- Always search for duplicates with jira_search before creating.\n"
            "- Use jira_list_projects to discover available projects and components.\n"
            "- Enrich descriptions with code context: affected files, functions, dependencies.\n"
            "- The agent MUST use ask_user to confirm the ticket details before calling this tool."
        ),
        "input_schema": JiraCreateIssueParams.model_json_schema(),
    },
    {
        "name": "jira_update_issue",
        "description": (
            "Update a Jira issue: transition status, add comment, append to description, or change fields.\n\n"
            "Usage:\n"
            "- Use transition_to to move a ticket between statuses (e.g. 'To Do' → 'In Progress').\n"
            "- SAFETY: Agent CANNOT transition to Done/Closed/Resolved — these require manual user action.\n"
            "- Add comments with code context to document investigation findings.\n"
            "- Use description_append to add Conductor analysis (affected files, change summary) to the ticket description. "
            "Original content is preserved — a separator and timestamp are inserted automatically.\n"
            "- When picking up a ticket to work on, transition it to 'To Do' or 'In Progress'."
        ),
        "input_schema": JiraUpdateIssueParams.model_json_schema(),
    },
    {
        "name": "jira_list_projects",
        "description": (
            "List available Jira projects, their issue types, and components.\n\n"
            "Usage:\n"
            "- Call once to discover project keys, names, and available components.\n"
            "- Results are filtered to active projects configured by the team.\n"
            "- Use before jira_create_issue to pick the right project and component."
        ),
        "input_schema": JiraListProjectsParams.model_json_schema(),
    },
    # ------------------------------------------------------------------
    # Browser tools
    # ------------------------------------------------------------------
    {
        "name": "web_search",
        "description": (
            "Search the web and return structured results with title, URL, and snippet.\n\n"
            "Usage:\n"
            "- Use to look up external library documentation, error messages, API references, "
            "or best practices.\n"
            "- Follow up with web_navigate on interesting URLs to read the full page.\n"
            "- Use grep instead when searching the local codebase — web_search is for external resources."
        ),
        "input_schema": WebSearchParams.model_json_schema(),
    },
    {
        "name": "web_navigate",
        "description": (
            "Navigate a headless browser to a URL and return the page content.\n\n"
            "Usage:\n"
            "- Use after web_search to read full pages of interest.\n"
            "- Returns page title, final URL (after redirects), visible text, and links.\n"
            "- The browser session persists across calls — you can navigate, click, fill, "
            "and extract in sequence.\n"
            "- Use web_extract after navigating for targeted data extraction."
        ),
        "input_schema": WebNavigateParams.model_json_schema(),
    },
    {
        "name": "web_click",
        "description": (
            "Click an element on the current browser page by CSS selector or visible text.\n\n"
            "Usage:\n"
            "- Use after web_navigate to interact with buttons, links, tabs, and other clickable elements.\n"
            "- Returns the page state after the click (URL, title, nearby text)."
        ),
        "input_schema": WebClickParams.model_json_schema(),
    },
    {
        "name": "web_fill",
        "description": (
            "Fill a form input on the current browser page.\n\n"
            "Usage:\n"
            "- Clears the field first, then types the value.\n"
            "- Set press_enter=true for search boxes that submit on Enter.\n"
            "- Use after web_navigate to interact with forms."
        ),
        "input_schema": WebFillParams.model_json_schema(),
    },
    {
        "name": "web_screenshot",
        "description": (
            "Take a screenshot of the current browser page or a specific element.\n\n"
            "Usage:\n"
            "- Returns the path to the saved PNG file.\n"
            "- Use when text extraction is insufficient and you need the visual layout.\n"
            "- Use web_extract instead when you can target elements by CSS selector."
        ),
        "input_schema": WebScreenshotParams.model_json_schema(),
    },
    {
        "name": "web_extract",
        "description": (
            "Extract text or attributes from elements matching a CSS selector on the current page.\n\n"
            "Usage:\n"
            "- Use to scrape structured data: tables, lists, or specific sections.\n"
            "- Returns an array of matches.\n"
            "- Use after web_navigate to target specific page content."
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
            "Provide 2-4 concrete options in the 'options' array — these are "
            "rendered as clickable buttons for the user. Mark your recommended "
            "option with '(recommended)' suffix. The user can also type a "
            "free-form answer. Use at most once per session."
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
# Tool metadata — structured properties for concurrency, context compaction,
# and future deferred loading.  Does NOT affect the LLM API contract; this is
# backend-only infrastructure.
# ---------------------------------------------------------------------------


@dataclass
class ToolMetadata:
    """Per-tool metadata for agent loop infrastructure."""

    is_read_only: bool = True
    is_concurrent_safe: bool = True
    summary_template: str = ""  # Python format string for context compaction
    category: str = "search"  # search | navigate | git | analysis | test | browser


TOOL_METADATA: Dict[str, ToolMetadata] = {
    # --- Search & Navigation ---
    "grep": ToolMetadata(category="search", summary_template="grep '{pattern}' in {path}: {_count} matches"),
    "read_file": ToolMetadata(category="navigate", summary_template="read {path} lines {start_line}-{end_line}"),
    "list_files": ToolMetadata(category="navigate", summary_template="listed {directory}: {_count} entries"),
    "glob": ToolMetadata(category="navigate", summary_template="glob '{pattern}': {_count} files"),
    "find_symbol": ToolMetadata(category="search", summary_template="find_symbol '{name}': {_count} definitions"),
    "find_references": ToolMetadata(
        category="search", summary_template="find_references '{symbol_name}': {_count} usages"
    ),
    "file_outline": ToolMetadata(category="navigate", summary_template="outline {path}: {_count} symbols"),
    "compressed_view": ToolMetadata(category="navigate", summary_template="compressed_view {file_path}"),
    "module_summary": ToolMetadata(category="navigate", summary_template="module_summary {module_path}"),
    "expand_symbol": ToolMetadata(category="navigate", summary_template="expand {symbol_name} in {file_path}"),
    # --- Dependency Analysis ---
    "get_dependencies": ToolMetadata(
        category="analysis", summary_template="dependencies of {file_path}: {_count} files"
    ),
    "get_dependents": ToolMetadata(category="analysis", summary_template="dependents of {file_path}: {_count} files"),
    # --- Call Graph ---
    "get_callees": ToolMetadata(category="analysis", summary_template="callees of {function_name}: {_count} calls"),
    "get_callers": ToolMetadata(category="analysis", summary_template="callers of {function_name}: {_count} callers"),
    "trace_variable": ToolMetadata(
        category="analysis", summary_template="trace {variable_name} {direction}: {_count} flows"
    ),
    # --- Git ---
    "git_log": ToolMetadata(category="git", summary_template="git_log: {_count} commits"),
    "git_diff": ToolMetadata(category="git", summary_template="git_diff {ref1}..{ref2} {file}"),
    "git_diff_files": ToolMetadata(category="git", summary_template="changed files {ref}: {_count} files"),
    "git_blame": ToolMetadata(category="git", summary_template="blame {file}: {_count} lines"),
    "git_show": ToolMetadata(category="git", summary_template="git_show {commit}"),
    "git_hotspots": ToolMetadata(category="git", summary_template="hotspots (last {days}d): {_count} files"),
    # --- Code Analysis ---
    "ast_search": ToolMetadata(category="analysis", summary_template="ast_search '{pattern}': {_count} matches"),
    "detect_patterns": ToolMetadata(
        category="analysis", summary_template="detect_patterns in {path}: {_count} patterns"
    ),
    "list_endpoints": ToolMetadata(category="analysis", summary_template="endpoints: {_count} routes"),
    "extract_docstrings": ToolMetadata(category="analysis", summary_template="docstrings in {path}: {_count} docs"),
    "db_schema": ToolMetadata(category="analysis", summary_template="db_schema: {_count} models"),
    # --- Testing ---
    "find_tests": ToolMetadata(category="test", summary_template="tests for '{name}': {_count} tests"),
    "test_outline": ToolMetadata(category="test", summary_template="test_outline {path}: {_count} tests"),
    "run_test": ToolMetadata(
        is_read_only=False, is_concurrent_safe=False, category="test", summary_template="ran {test_file}: {_status}"
    ),
    # --- File Editing ---
    "file_edit": ToolMetadata(
        is_read_only=False,
        is_concurrent_safe=False,
        category="edit",
        summary_template="file_edit {path}: {replacements} replacement(s)",
    ),
    "file_write": ToolMetadata(
        is_read_only=False, is_concurrent_safe=False, category="edit", summary_template="file_write {path}: {action}"
    ),
    # --- Jira Integration ---
    "jira_search": ToolMetadata(category="integration", summary_template="jira_search '{query}': {_count} issues"),
    "jira_get_issue": ToolMetadata(
        category="integration",
        summary_template="jira_get_issue {issue_key}: {summary} | status={status} priority={priority} | {_description_preview}",
    ),
    "jira_create_issue": ToolMetadata(
        is_read_only=False,
        is_concurrent_safe=False,
        category="integration",
        summary_template="jira_create_issue: created {_result}",
    ),
    "jira_list_projects": ToolMetadata(
        category="integration", summary_template="jira_list_projects: {_count} projects"
    ),
    "jira_update_issue": ToolMetadata(
        is_read_only=False,
        is_concurrent_safe=False,
        category="integration",
        summary_template="jira_update_issue {issue_key}: {_action}",
    ),
    # --- Browser ---
    "web_search": ToolMetadata(
        is_read_only=False,
        is_concurrent_safe=False,
        category="browser",
        summary_template="web_search '{query}': {_count} results",
    ),
    "web_navigate": ToolMetadata(
        is_read_only=False, is_concurrent_safe=False, category="browser", summary_template="navigated to {url}"
    ),
    "web_click": ToolMetadata(
        is_read_only=False, is_concurrent_safe=False, category="browser", summary_template="clicked '{selector}'"
    ),
    "web_fill": ToolMetadata(
        is_read_only=False, is_concurrent_safe=False, category="browser", summary_template="filled '{selector}'"
    ),
    "web_screenshot": ToolMetadata(
        is_read_only=False, is_concurrent_safe=False, category="browser", summary_template="screenshot taken"
    ),
    "web_extract": ToolMetadata(
        is_read_only=False,
        is_concurrent_safe=False,
        category="browser",
        summary_template="extracted '{selector}': {_count} elements",
    ),
}


def get_tool_metadata(tool_name: str) -> ToolMetadata:
    """Return metadata for a tool, defaulting to read-only/concurrent-safe."""
    return TOOL_METADATA.get(tool_name, ToolMetadata())


def format_tool_summary(tool_name: str, params: Dict[str, Any], result_data: Any) -> str:
    """Format a one-line summary of a tool call for context compaction.

    Uses the tool's summary_template, filling in params and a computed _count.
    Falls back to a generic summary if the template is missing or formatting fails.
    """
    meta = TOOL_METADATA.get(tool_name)
    if not meta or not meta.summary_template:
        return f"{tool_name}()"

    # Compute _count from result data
    _count = len(result_data) if isinstance(result_data, list) else 0
    # Compute _status for run_test
    _status = "unknown"
    _description_preview = ""
    if isinstance(result_data, dict):
        _status = result_data.get("status", result_data.get("result", "done"))
        # For jira_get_issue: preserve description preview in summary
        desc = result_data.get("description", "")
        if desc:
            _description_preview = desc[:500].replace("\n", " ")
        # Merge result_data fields into template vars so Jira summaries work
        _count = _count or len(result_data.get("issues", []))

    template_vars = {
        **params,
        **({k: v for k, v in result_data.items() if isinstance(v, str)} if isinstance(result_data, dict) else {}),
        "_count": _count,
        "_status": _status,
        "_description_preview": _description_preview,
    }
    # Fill missing keys with empty strings to avoid KeyError
    try:
        return meta.summary_template.format_map(
            {k: template_vars.get(k, "") for k in _extract_format_keys(meta.summary_template)}
        )
    except (KeyError, ValueError, IndexError):
        return f"{tool_name}()"


def _extract_format_keys(template: str) -> List[str]:
    """Extract {key} names from a format string."""
    import re as _re

    return _re.findall(r"\{(\w+)\}", template)


# ---------------------------------------------------------------------------
# Brain orchestrator tool definitions (separate from TOOL_DEFINITIONS)
#
# These are meta-tools for the Brain agent only — they dispatch sub-agents
# and evaluate findings. Never exposed to regular explorer/review agents,
# never included in parity tests, never proxied to the VS Code extension.
# ---------------------------------------------------------------------------

BRAIN_TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "name": "create_plan",
        "description": (
            "Create an investigation plan before dispatching agents. "
            "Call this FIRST to declare your dispatch mode, which agents "
            "to use, and why. The plan is shown to the user for transparency."
        ),
        "input_schema": CreatePlanParams.model_json_schema(),
    },
    {
        "name": "dispatch_agent",
        "description": (
            "Dispatch an agent to investigate the codebase. Two modes:\n"
            "1. Template mode: set template= to use a pre-defined agent "
            "(for PR review swarm and business flow swarm agents only).\n"
            "2. Dynamic mode: set tools= and optionally perspective=, skill=, "
            "model=, budget_tokens= to compose an agent on the fly. "
            "Use the skill catalog and tool catalog in your system prompt "
            "to select the right combination."
        ),
        "input_schema": DispatchAgentParams.model_json_schema(),
    },
    {
        "name": "dispatch_swarm",
        "description": (
            "Dispatch a predefined group of parallel agents. Only use for "
            "end-to-end business flow tracing: 'business_flow' (2-agent "
            "flow tracing). For PR reviews use transfer_to_brain instead. "
            "For all other tasks, use dispatch_agent."
        ),
        "input_schema": DispatchSwarmParams.model_json_schema(),
    },
    {
        "name": "transfer_to_brain",
        "description": (
            "Transfer control to a specialized Brain orchestrator. "
            "Use for PR reviews: transfer_to_brain(brain_name='pr_review'). "
            "The specialized Brain takes over entirely with its own pipeline — "
            "pre-computed context, parallel review agents, arbitration, and synthesis. "
            "You will NOT get control back. One-way handoff."
        ),
        "input_schema": TransferToBrainParams.model_json_schema(),
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
