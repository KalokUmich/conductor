// Auto-generated from Python Pydantic models. Do not edit manually.
// Regenerate with: python scripts/generate_tool_contracts.py

// ---- Result models ----

export interface GrepMatch {
    file_path: string;
    line_number: number;
    content: string;
}

export interface SymbolLocation {
    name: string;
    kind: string;
    file_path: string;
    start_line: number;
    end_line: number;
    signature?: string;
}

export interface ReferenceLocation {
    file_path: string;
    line_number: number;
    content: string;
}

export interface FileEntry {
    path: string;
    is_dir: boolean;
    size?: number;
}

export interface AstMatch {
    file_path: string;
    start_line: number;
    end_line: number;
    text: string;
    meta_variables?: Record<string, string>;
}

export interface CallerInfo {
    caller_name: string;
    caller_kind: string;
    file_path: string;
    line: number;
    content: string;
}

export interface CalleeInfo {
    callee_name: string;
    file_path: string;
    line: number;
}

export interface DependencyInfo {
    file_path: string;
    symbols?: string[];
    weight?: number;
}

export interface GitCommit {
    hash: string;
    message: string;
    author?: string;
    date?: string;
}

export interface DiffFileEntry {
    path: string;
    status: string;
    additions?: number;
    deletions?: number;
    old_path?: string;
}

export interface BlameEntry {
    commit_hash: string;
    author: string;
    date: string;
    line_number: number;
    content: string;
}

export interface TestMatch {
    test_file: string;
    test_function: string;
    line_number: number;
    context?: string;
}

export interface TestOutlineEntry {
    name: string;
    kind: string;
    line_number: number;
    end_line?: number;
    mocks?: string[];
    assertions?: string[];
    fixtures?: string[];
}

// ---- Dict-shaped tool outputs ----

export interface ReadFileResult {
    path: string;
    total_lines: number;
    content: string;
}

export interface GlobItem {
    path: string;
    size: string;
}

export interface GitDiffResult {
    diff: string;
}

export interface GitShowResult {
    commit_hash: string;
    author: string;
    date: string;
    message: string;
    diff: string;
}

export interface TraceVariableResult {
    variable: string;
    file: string;
    function: string;
    direction: string;
}

export interface CompressedViewResult {
    content: string;
    path: string;
    total_lines: number;
    symbol_count: number;
}

export interface ModuleSummaryResult {
    content: string;
    file_count: number;
    loc: number;
}

export interface ExpandSymbolResult {
    symbol_name: string;
    kind: string;
    file_path: string;
    start_line: number;
    end_line: number;
    signature: string;
    source: string;
}

export interface DetectPatternsResult {
    summary: string;
    total_matches: string;
    categories_scanned: string;
    files_scanned: string;
    matches: string;
}

export interface RunTestResult {
    passed: boolean;
    return_code: string;
    runner: string;
    test_file: string;
    test_name: string;
    output: string;
    stderr: string;
}

// ---- Param models ----

export interface GrepParams {
    pattern: string;
    path?: string;
    include_glob?: string;
    max_results?: number;
    output_mode?: string;
    context_lines?: number;
    case_insensitive?: boolean;
    multiline?: boolean;
    file_type?: string;
}

export interface ReadFileParams {
    path: string;
    start_line?: number;
    end_line?: number;
}

export interface ListFilesParams {
    directory?: string;
    max_depth?: number;
    include_glob?: string;
}

export interface GlobParams {
    pattern: string;
    path?: string;
}

export interface FindSymbolParams {
    name: string;
    kind?: string;
}

export interface FindReferencesParams {
    symbol_name: string;
    file?: string;
}

export interface FileOutlineParams {
    path: string;
}

export interface GetDependenciesParams {
    file_path: string;
    max_depth?: number;
}

export interface GetDependentsParams {
    file_path: string;
    max_depth?: number;
}

export interface GitLogParams {
    file?: string;
    n?: number;
    search?: string;
}

export interface GitDiffParams {
    ref1?: string;
    ref2?: string;
    file?: string;
    context_lines?: number;
}

export interface GitDiffFilesParams {
    ref: string;
}

export interface AstSearchParams {
    pattern: string;
    language?: string;
    path?: string;
    max_results?: number;
}

export interface GetCalleesParams {
    function_name: string;
    file: string;
}

export interface GetCallersParams {
    function_name: string;
    path?: string;
}

export interface GitBlameParams {
    file: string;
    start_line?: number;
    end_line?: number;
}

export interface GitShowParams {
    commit: string;
    file?: string;
}

export interface FindTestsParams {
    name: string;
    path?: string;
}

export interface TestOutlineParams {
    path: string;
}

export interface TraceVariableParams {
    variable_name: string;
    file: string;
    function_name?: string;
    direction?: string;
}

export interface CompressedViewParams {
    file_path: string;
    focus?: string;
}

export interface ModuleSummaryParams {
    module_path: string;
}

export interface ExpandSymbolParams {
    symbol_name: string;
    file_path?: string;
}

export interface DetectPatternsParams {
    path?: string;
    categories?: string[];
    max_results?: number;
}

export interface RunTestParams {
    test_file: string;
    test_name?: string;
    timeout?: number;
}

// ---- Tool output type map ----

export interface ToolOutputMap {
    grep: GrepMatch[];
    read_file: ReadFileResult;
    list_files: FileEntry[];
    glob: GlobItem[];
    find_symbol: SymbolLocation[];
    find_references: ReferenceLocation[];
    file_outline: SymbolLocation[];
    get_dependencies: DependencyInfo[];
    get_dependents: DependencyInfo[];
    git_log: GitCommit[];
    git_diff: GitDiffResult;
    git_diff_files: DiffFileEntry[];
    ast_search: AstMatch[];
    get_callees: CalleeInfo[];
    get_callers: CallerInfo[];
    git_blame: BlameEntry[];
    git_show: GitShowResult;
    find_tests: TestMatch[];
    test_outline: TestOutlineEntry[];
    trace_variable: TraceVariableResult;
    compressed_view: CompressedViewResult;
    module_summary: ModuleSummaryResult;
    expand_symbol: ExpandSymbolResult;
    detect_patterns: DetectPatternsResult;
    run_test: RunTestResult;
}
