#!/usr/bin/env node
/**
 * CLI runner for ALL standalone TypeScript tools — used by agent quality eval.
 *
 * Supports 13 tools:
 *   Complex (7): get_dependencies, get_dependents, test_outline,
 *                compressed_view, trace_variable, detect_patterns, module_summary
 *   AST (6):     file_outline, find_symbol, find_references,
 *                get_callees, get_callers, expand_symbol
 *
 * AST tools use web-tree-sitter WASM for parsing (same quality as Python backend).
 * Tree-sitter is initialized automatically from extension/grammars/.
 *
 * Usage:
 *   node tests/run_ts_tool.js <tool_name> <workspace> '<json_params>'
 *   node tests/run_ts_tool.js list                    # list available tools
 *
 * Outputs a JSON object to stdout matching the ToolResult interface:
 *   { "success": bool, "data": any, "error": string|null, "truncated": bool }
 *
 * Exit code 0 always (errors are in the JSON output).
 */

const path = require('path');

const outDir = path.join(__dirname, '..', 'out', 'services');
const extensionDir = path.join(__dirname, '..');

// ---------------------------------------------------------------------------
// Load compiled modules
// ---------------------------------------------------------------------------

let complexTools, astTools, treeSitter;
try {
    complexTools = require(path.join(outDir, 'complexToolRunner'));
} catch (e) {
    outputError(`Cannot load complexToolRunner: ${e.message}. Run 'npm run compile' first.`);
    process.exit(0);
}

try {
    astTools = require(path.join(outDir, 'astToolRunner'));
} catch (e) {
    outputError(`Cannot load astToolRunner: ${e.message}. Run 'npm run compile' first.`);
    process.exit(0);
}

try {
    treeSitter = require(path.join(outDir, 'treeSitterService'));
} catch (e) {
    // Tree-sitter is optional — AST tools fall back to regex extraction
    treeSitter = null;
}

// ---------------------------------------------------------------------------
// Tool registry
// ---------------------------------------------------------------------------

const COMPLEX_TOOLS = {
    get_dependencies: complexTools.get_dependencies,
    get_dependents: complexTools.get_dependents,
    test_outline: complexTools.test_outline,
    compressed_view: complexTools.compressed_view,
    trace_variable: complexTools.trace_variable,
    detect_patterns: complexTools.detect_patterns,
    module_summary: complexTools.module_summary,
};

const AST_TOOLS = {
    file_outline: astTools.file_outline,
    find_symbol: astTools.find_symbol,
    find_references: astTools.find_references,
    get_callees: astTools.get_callees,
    get_callers: astTools.get_callers,
    expand_symbol: astTools.expand_symbol,
};

const ALL_TOOLS = { ...COMPLEX_TOOLS, ...AST_TOOLS };

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function outputError(msg) {
    console.log(JSON.stringify({ success: false, data: null, error: msg }));
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main() {
    const [,, toolName, workspace, paramsJson] = process.argv;

    // List mode
    if (toolName === 'list') {
        console.log(JSON.stringify({
            complex: Object.keys(COMPLEX_TOOLS),
            ast: Object.keys(AST_TOOLS),
        }));
        return;
    }

    if (!toolName || !workspace) {
        outputError("Usage: node run_ts_tool.js <tool> <workspace> '<json_params>'");
        return;
    }

    let params = {};
    if (paramsJson) {
        try {
            params = JSON.parse(paramsJson);
        } catch (e) {
            outputError(`Invalid JSON params: ${e.message}`);
            return;
        }
    }

    const toolFn = ALL_TOOLS[toolName];
    if (typeof toolFn !== 'function') {
        outputError(`Unknown tool: ${toolName}. Available: ${Object.keys(ALL_TOOLS).join(', ')}`);
        return;
    }

    // Initialize tree-sitter for AST tools and module_summary (best-effort — falls back to regex)
    const needsTreeSitter = toolName in AST_TOOLS || toolName === 'module_summary';
    if (needsTreeSitter && treeSitter && !treeSitter.isInitialized()) {
        try {
            await treeSitter.initTreeSitter(extensionDir);
        } catch (e) {
            // Proceed without tree-sitter — regex fallback will be used
            process.stderr.write(`[warn] tree-sitter init failed: ${e.message}\n`);
        }
    }

    try {
        const result = await toolFn(workspace, params);
        console.log(JSON.stringify(result));
    } catch (e) {
        outputError(`Tool threw: ${e.message || e}`);
    }
}

main().catch(e => {
    outputError(`Runner crashed: ${e.message || e}`);
});
