#!/usr/bin/env node
/**
 * CLI runner for TypeScript complex tools — used by Python parity tests.
 *
 * Usage:
 *   node tests/run_complex_tool.js <tool_name> <workspace> '<json_params>'
 *
 * Outputs a JSON object to stdout matching the ToolResult interface:
 *   { "success": bool, "data": any, "error": string|null }
 *
 * Exit code 0 always (errors are in the JSON output).
 */

const path = require('path');

// Load the compiled complexToolRunner from the extension's out/ directory
const outDir = path.join(__dirname, '..', 'out', 'services');
let complexTools;
try {
    complexTools = require(path.join(outDir, 'complexToolRunner'));
} catch (e) {
    // If out/ doesn't exist, try loading from src/ via ts-node or tsx
    console.error(JSON.stringify({
        success: false,
        data: null,
        error: `Cannot load complexToolRunner: ${e.message}. Run 'npm run compile' first.`,
    }));
    process.exit(0);
}

// Parse arguments
const [,, toolName, workspace, paramsJson] = process.argv;

if (!toolName || !workspace) {
    console.error(JSON.stringify({
        success: false,
        data: null,
        error: 'Usage: node run_complex_tool.js <tool> <workspace> \'<json_params>\'',
    }));
    process.exit(0);
}

let params = {};
if (paramsJson) {
    try {
        params = JSON.parse(paramsJson);
    } catch (e) {
        console.error(JSON.stringify({
            success: false,
            data: null,
            error: `Invalid JSON params: ${e.message}`,
        }));
        process.exit(0);
    }
}

// Route to the correct tool function
const toolFn = complexTools[toolName];
if (typeof toolFn !== 'function') {
    console.log(JSON.stringify({
        success: false,
        data: null,
        error: `Unknown tool: ${toolName}. Available: ${Object.keys(complexTools).filter(k => typeof complexTools[k] === 'function').join(', ')}`,
    }));
    process.exit(0);
}

async function run() {
    try {
        const result = await toolFn(workspace, params);
        console.log(JSON.stringify(result));
    } catch (e) {
        console.log(JSON.stringify({
            success: false,
            data: null,
            error: `Tool threw: ${e.message || e}`,
        }));
    }
}
run();
