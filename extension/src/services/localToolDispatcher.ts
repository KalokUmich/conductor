/**
 * Three-tier local tool dispatcher for Conductor.
 *
 * All 24 code tools run entirely within the extension — no Python
 * dependency required. Routing:
 *
 *   Tier 1 — SUBPROCESS_TOOLS: Simple CLI tools (grep/rg, git, find, fs)
 *            executed via child_process in the legacy _executeLocalTool.
 *
 *   Tier 2 — AST_TOOLS: Symbol-aware tools using web-tree-sitter WASM
 *            (astToolRunner.ts). Falls back to subprocess on failure.
 *
 *   Tier 3 — COMPLEX_TOOLS: Analysis tools (compressed_view, trace_variable,
 *            detect_patterns, etc.) implemented natively in TypeScript
 *            (complexToolRunner.ts). Falls back to subprocess on failure.
 *
 * @module services/localToolDispatcher
 */

import * as complexTools from './complexToolRunner';
import type { ToolResult } from './toolTypes';
export type { ToolResult };

// ---------------------------------------------------------------------------
// Tool classification sets
// ---------------------------------------------------------------------------

/** Tier 1: Tools that run as simple subprocesses (grep, git, find, fs). */
const SUBPROCESS_TOOLS = new Set([
    'grep',
    'read_file',
    'list_files',
    'glob',
    'git_log',
    'git_diff',
    'git_diff_files',
    'git_blame',
    'git_show',
    'find_tests',
    'run_test',
    'ast_search',
    'get_repo_graph',
]);

/** Tier 2: Symbol-aware tools that benefit from VS Code LSP / AST analysis. */
const AST_TOOLS = new Set([
    'find_symbol',
    'find_references',
    'file_outline',
    'get_callees',
    'get_callers',
    'expand_symbol',
]);

/** Tier 3: Complex analysis tools — native TypeScript implementations. */
const COMPLEX_TOOLS = new Set([
    'trace_variable',
    'compressed_view',
    'detect_patterns',
    'get_dependencies',
    'get_dependents',
    'test_outline',
    'module_summary',
]);

/**
 * Backend-only tools — require server-side resources (e.g. Playwright browser).
 * These are never executed in the extension; the backend's RemoteToolExecutor
 * intercepts them before proxying.
 */
const BACKEND_ONLY_TOOLS = new Set([
    'web_search',
    'web_navigate',
    'web_click',
    'web_fill',
    'web_screenshot',
    'web_extract',
]);

/**
 * Map of complex tool names to their TypeScript implementation functions.
 * These run in-process — no Python CLI, no subprocess.
 */
const COMPLEX_TOOL_RUNNERS: Record<string, (workspace: string, params: any) => complexTools.ToolResult | Promise<complexTools.ToolResult>> = {
    get_dependencies: complexTools.get_dependencies,
    get_dependents: complexTools.get_dependents,
    test_outline: complexTools.test_outline,
    compressed_view: complexTools.compressed_view,
    trace_variable: complexTools.trace_variable,
    detect_patterns: complexTools.detect_patterns,
    module_summary: complexTools.module_summary,
};

// ---------------------------------------------------------------------------
// Extension context passed by the caller
// ---------------------------------------------------------------------------

/**
 * Contextual helpers provided by the extension host.
 *
 * - `extensionPath`: Absolute path to the extension's install directory.
 * - `lspHelpers`: Object containing VS Code LSP helper functions
 *   (e.g. getDocumentSymbols, findReferencesLsp, prepareCallHierarchy,
 *    flattenSymbols, toUri, toRelative). These are the same helpers
 *   defined inside the existing `_executeLocalTool` method.
 */
export interface ExtensionContext {
    extensionPath: string;
    lspHelpers: any;
}

// ---------------------------------------------------------------------------
// Callback types
// ---------------------------------------------------------------------------

/**
 * Callback for the existing subprocess-based tool execution.
 *
 * This is the original `_executeLocalTool` logic for Tier 1 tools and as
 * a final fallback for Tier 2 / Tier 3 tools when the preferred strategy
 * fails.
 */
export type SubprocessCallback = (
    tool: string,
    params: any,
    workspace: string,
) => Promise<ToolResult>;

/**
 * Callback for AST/LSP-based tool execution.
 *
 * Attempts to fulfil the tool request using VS Code Language Services
 * (Document Symbols, Reference Provider, Call Hierarchy, etc.).
 * Returns `null` if LSP data is unavailable and the dispatcher should
 * fall through to the next tier.
 */
export type AstToolRunner = (
    tool: string,
    params: any,
    workspace: string,
    lspHelpers: any,
) => Promise<ToolResult | null>;

// ---------------------------------------------------------------------------
// Logger
// ---------------------------------------------------------------------------

/** Lazy-loaded VS Code output channel for dispatcher logging. */
let outputChannel: any;

function log(message: string): void {
    // Try VS Code output channel first
    try {
        if (!outputChannel) {
            const vscode = require('vscode');
            outputChannel = vscode.window.createOutputChannel('Conductor Tools');
        }
        outputChannel.appendLine(`[dispatcher] ${message}`);
    } catch {
        // VS Code API not available (e.g. in tests) — fall back to console.
        console.log(`[conductor-dispatcher] ${message}`);
    }
}

// ---------------------------------------------------------------------------
// Main dispatcher
// ---------------------------------------------------------------------------

/**
 * Execute a local code tool using the three-tier routing strategy.
 *
 * @param tool             Tool name (e.g. 'grep', 'find_symbol', 'trace_variable').
 * @param params           Tool parameters as a plain object.
 * @param workspace        Absolute path to the workspace root.
 * @param extensionContext Extension-provided context (extensionPath, LSP helpers).
 * @param subprocessFn     Callback to the existing subprocess implementation.
 * @param astRunnerFn      Optional callback for AST/LSP-based tool execution.
 * @returns A ToolResult — always resolves (never rejects).
 */
export async function executeLocalTool(
    tool: string,
    params: any,
    workspace: string,
    extensionContext: ExtensionContext,
    subprocessFn: SubprocessCallback,
    astRunnerFn?: AstToolRunner,
): Promise<ToolResult> {

    // ---- Tier 1: Subprocess tools ----
    if (SUBPROCESS_TOOLS.has(tool)) {
        log(`${tool} → Tier 1 (subprocess)`);
        return subprocessFn(tool, params, workspace);
    }

    // ---- Tier 2: AST / LSP tools ----
    if (AST_TOOLS.has(tool)) {
        // 2a. Try AST/LSP runner
        if (astRunnerFn) {
            try {
                log(`${tool} → Tier 2a (AST/LSP)`);
                const result = await astRunnerFn(
                    tool,
                    params,
                    workspace,
                    extensionContext.lspHelpers,
                );
                if (result !== null) {
                    log(`${tool} → Tier 2a succeeded`);
                    return result;
                }
                log(`${tool} → Tier 2a returned null, trying Python CLI`);
            } catch (err: unknown) {
                log(`${tool} → Tier 2a failed: ${err instanceof Error ? err.message : String(err)}`);
            }
        }

        // 2b. Final fallback to subprocess (existing grep-based implementation)
        log(`${tool} → Tier 2b (subprocess fallback)`);
        return subprocessFn(tool, params, workspace);
    }

    // ---- Tier 3: Complex tools — native TypeScript ----
    if (COMPLEX_TOOLS.has(tool)) {
        const runner = COMPLEX_TOOL_RUNNERS[tool];
        if (runner) {
            try {
                log(`${tool} → Tier 3 (native TS)`);
                const result = await runner(workspace, params);
                if (result.success) {
                    log(`${tool} → Tier 3 succeeded`);
                    return result;
                }
                log(`${tool} → Tier 3 failed: ${result.error}, trying subprocess fallback`);
            } catch (err: unknown) {
                log(`${tool} → Tier 3 error: ${err instanceof Error ? err.message : String(err)}`);
            }
        }

        // Fallback to subprocess (existing simplified implementation)
        log(`${tool} → Tier 3 fallback (subprocess)`);
        return subprocessFn(tool, params, workspace);
    }

    // ---- Backend-only tools (e.g. Playwright browser tools) ----
    if (BACKEND_ONLY_TOOLS.has(tool)) {
        log(`${tool} → backend-only tool, skipping local dispatch`);
        return {
            success: false,
            data: null,
            error: `Tool '${tool}' is a backend-only tool and cannot run in the extension`,
        };
    }

    // ---- Unknown tool ----
    log(`${tool} → unknown tool, returning error`);
    return {
        success: false,
        data: null,
        error: `Tool '${tool}' is not recognised by the local tool dispatcher`,
    };
}

// ---------------------------------------------------------------------------
// Utility: classify a tool name
// ---------------------------------------------------------------------------

/** Returns which tier a tool belongs to, or 'unknown'. */
export function classifyTool(
    tool: string,
): 'subprocess' | 'ast' | 'complex' | 'backend_only' | 'unknown' {
    if (SUBPROCESS_TOOLS.has(tool)) { return 'subprocess'; }
    if (AST_TOOLS.has(tool)) { return 'ast'; }
    if (COMPLEX_TOOLS.has(tool)) { return 'complex'; }
    if (BACKEND_ONLY_TOOLS.has(tool)) { return 'backend_only'; }
    return 'unknown';
}

/** Returns the full set of tools the dispatcher can handle. */
export function supportedTools(): string[] {
    return [
        ...Array.from(SUBPROCESS_TOOLS),
        ...Array.from(AST_TOOLS),
        ...Array.from(COMPLEX_TOOLS),
    ];
}
