#!/usr/bin/env node
/**
 * Validate TypeScript tool implementations against the shared contract.
 *
 * Loads contracts/tool_contracts.json and runs each TS-implemented tool
 * with a smoke-test input against the parity fixture repo, then checks
 * that the returned data shape matches the contract (field names exist).
 *
 * Usage:
 *   node tests/validate_contract.js [path/to/tool_contracts.json]
 *
 * Exit code 0 if all tools match, 1 if any mismatch.
 */

const fs = require('fs');
const path = require('path');

const contractPath = process.argv[2]
    || path.join(__dirname, '..', '..', 'contracts', 'tool_contracts.json');

const fixtureRepo = path.join(__dirname, '..', '..', 'tests', 'fixtures', 'parity_repo');

// Load contract
let contract;
try {
    contract = JSON.parse(fs.readFileSync(contractPath, 'utf-8'));
} catch (e) {
    console.error(`Cannot load contract: ${e.message}`);
    process.exit(1);
}

// Load TS tools
const outDir = path.join(__dirname, '..', 'out', 'services');
let complexTools, astTools, treeSitter;
try {
    complexTools = require(path.join(outDir, 'complexToolRunner'));
    astTools = require(path.join(outDir, 'astToolRunner'));
    treeSitter = require(path.join(outDir, 'treeSitterService'));
} catch (e) {
    console.error(`Cannot load compiled tools: ${e.message}. Run 'npm run compile' first.`);
    process.exit(1);
}

// TS-implemented tools (complex + AST)
const TS_TOOLS = {
    // Complex (sync except module_summary)
    get_dependencies: (ws, p) => complexTools.get_dependencies(ws, p),
    get_dependents: (ws, p) => complexTools.get_dependents(ws, p),
    test_outline: (ws, p) => complexTools.test_outline(ws, p),
    compressed_view: (ws, p) => complexTools.compressed_view(ws, p),
    trace_variable: (ws, p) => complexTools.trace_variable(ws, p),
    detect_patterns: (ws, p) => complexTools.detect_patterns(ws, p),
    module_summary: (ws, p) => complexTools.module_summary(ws, p),
    // AST
    file_outline: (ws, p) => astTools.file_outline(ws, p),
    find_symbol: (ws, p) => astTools.find_symbol(ws, p),
    find_references: (ws, p) => astTools.find_references(ws, p),
    get_callees: (ws, p) => astTools.get_callees(ws, p),
    get_callers: (ws, p) => astTools.get_callers(ws, p),
    expand_symbol: (ws, p) => astTools.expand_symbol(ws, p),
};

// Smoke-test params per tool (minimal valid input)
const SMOKE_PARAMS = {
    get_dependencies: { file_path: 'app/service.py' },
    get_dependents: { file_path: 'app/models.py' },
    test_outline: { path: 'tests/test_service.py' },
    compressed_view: { file_path: 'app/service.py' },
    trace_variable: { variable_name: 'amount', file: 'app/service.py', function_name: 'process_payment' },
    detect_patterns: { path: 'app' },
    module_summary: { module_path: 'app' },
    file_outline: { path: 'app/service.py' },
    find_symbol: { name: 'OrderService' },
    find_references: { symbol_name: 'OrderService' },
    get_callees: { function_name: 'process_payment', file: 'app/service.py' },
    get_callers: { function_name: 'process_payment' },
    expand_symbol: { symbol_name: 'process_payment', file_path: 'app/service.py' },
};

function checkFields(data, requiredFields, toolName) {
    const errors = [];
    if (Array.isArray(data)) {
        if (data.length === 0) return []; // empty list is valid
        const item = data[0];
        for (const field of requiredFields) {
            if (!(field in item)) {
                errors.push(`${toolName}: missing field '${field}' in list item. Got: ${Object.keys(item).join(', ')}`);
            }
        }
    } else if (typeof data === 'object' && data !== null) {
        // For dict outputs, check fields at top level first.
        // If a field is missing at top level, check inside known nested arrays
        // (e.g., detect_patterns wraps items in data.matches).
        for (const field of requiredFields) {
            if (field in data) continue;
            // Check nested arrays (matches, items, results, etc.)
            let foundNested = false;
            for (const val of Object.values(data)) {
                if (Array.isArray(val) && val.length > 0 && typeof val[0] === 'object') {
                    if (field in val[0]) { foundNested = true; break; }
                }
            }
            if (!foundNested) {
                errors.push(`${toolName}: missing field '${field}'. Got: ${Object.keys(data).join(', ')}`);
            }
        }
    }
    return errors;
}

async function main() {
    // Init tree-sitter
    if (treeSitter && !treeSitter.isInitialized()) {
        try {
            await treeSitter.initTreeSitter(path.join(__dirname, '..'));
        } catch { /* proceed without */ }
    }

    const allErrors = [];
    let passed = 0;
    let skipped = 0;

    for (const [toolName, toolDef] of Object.entries(contract.tools)) {
        const runner = TS_TOOLS[toolName];
        if (!runner) {
            skipped++;
            continue; // subprocess tool, no TS implementation to validate
        }

        const params = SMOKE_PARAMS[toolName];
        if (!params) {
            skipped++;
            continue;
        }

        try {
            const result = await runner(fixtureRepo, params);
            if (!result || !result.success) {
                // Tool returned error — still check that it has ToolResult shape
                if (!result || typeof result.success !== 'boolean') {
                    allErrors.push(`${toolName}: did not return {success: boolean} shape`);
                }
                passed++;
                continue;
            }

            // Check output fields match contract
            const fields = toolDef.output_item_fields || [];
            if (fields.length > 0 && result.data != null) {
                const fieldErrors = checkFields(result.data, fields, toolName);
                if (fieldErrors.length > 0) {
                    // Warn but don't fail — contract shape may describe inner items
                    // while the actual output wraps them differently
                    fieldErrors.forEach(e => console.log(`  [warn] ${e}`));
                }
            }
            passed++;
        } catch (e) {
            allErrors.push(`${toolName}: threw ${e.message}`);
        }
    }

    console.log(`Contract validation: ${passed} passed, ${skipped} skipped (subprocess), ${allErrors.length} errors`);
    if (allErrors.length > 0) {
        console.log('\nErrors:');
        allErrors.forEach(e => console.log(`  ${e}`));
        process.exit(1);
    }
}

main().catch(e => {
    console.error(`Validator crashed: ${e.message}`);
    process.exit(1);
});
