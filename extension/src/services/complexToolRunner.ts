/**
 * Complex code intelligence tools for Conductor (TypeScript implementation).
 *
 * Ports 7 tools from the Python backend (code_tools/tools.py) to TypeScript
 * so the extension can run ALL tools locally without a Python dependency:
 *
 *   - get_dependencies    — find files this file imports
 *   - get_dependents      — find files that import this file
 *   - test_outline        — extract test structure (classes, functions, mocks, assertions)
 *   - compressed_view     — file signatures + callees + side effects (~80% token savings)
 *   - trace_variable      — data flow tracing (aliases, flows_to, sinks, sources)
 *   - detect_patterns     — architectural pattern detection (webhook, queue, retry, etc.)
 *   - module_summary      — high-level module overview (~95% token savings)
 *
 * All tools accept (workspace, params) and return a ToolResult, matching the
 * Python backend's interface exactly for parity testing.
 *
 * @module services/complexToolRunner
 */

import * as fs from 'fs';
import * as path from 'path';
import * as treeSitter from './treeSitterService';
import type { ToolResult } from './toolTypes';

// Re-export ToolResult so existing consumers of complexToolRunner still compile.
export type { ToolResult };

// ---------------------------------------------------------------------------
// Constants (shared with astToolRunner.ts)
// ---------------------------------------------------------------------------

const MAX_FILE_SIZE = 512_000;

const EXCLUDED_DIRS = new Set([
    '.git', '.hg', '.svn', '__pycache__', 'node_modules', 'target',
    'dist', 'vendor', '.venv', 'venv', '.mypy_cache', '.pytest_cache',
    '.tox', 'build', '.next', '.nuxt', '.yarn', '.pnp',
]);

const SOURCE_EXTS = new Set([
    '.py', '.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs',
    '.java', '.go', '.rs', '.rb', '.cs', '.cpp', '.cc', '.c', '.h',
]);

// Must match Python's _LANG_EXTS in code_tools/tools.py for module_summary parity
const MODULE_SUMMARY_EXTS = new Set([
    '.py', '.js', '.jsx', '.ts', '.tsx',
    '.java', '.go', '.rs', '.c', '.cpp',
]);

// Broader set for detect_patterns (includes config files)
const SCANNABLE_EXTS = new Set([
    ...SOURCE_EXTS,
    '.kt', '.scala', '.php',
    '.yaml', '.yml', '.toml', '.properties',
]);

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

function resolvePath(workspace: string, relPath: string): string {
    const ws = path.resolve(workspace);
    const target = path.resolve(ws, relPath);
    if (!target.startsWith(ws + path.sep) && target !== ws) {
        throw new Error(`Path escapes workspace: ${relPath}`);
    }
    return target;
}

function isExcluded(parts: string[]): boolean {
    return parts.some(p => EXCLUDED_DIRS.has(p));
}

function detectLanguage(filePath: string): string | null {
    const ext = path.extname(filePath).toLowerCase();
    const map: Record<string, string> = {
        '.py': 'python', '.js': 'javascript', '.jsx': 'javascript',
        '.ts': 'typescript', '.tsx': 'typescript', '.mjs': 'javascript',
        '.cjs': 'javascript', '.java': 'java', '.go': 'go', '.rs': 'rust',
        '.rb': 'ruby', '.cs': 'csharp', '.cpp': 'cpp', '.cc': 'cpp',
        '.c': 'c', '.h': 'c',
    };
    return map[ext] || null;
}

function readFileText(absPath: string): string | null {
    try {
        const stat = fs.statSync(absPath);
        if (!stat.isFile() || stat.size > MAX_FILE_SIZE) { return null; }
        // Normalize \r\n → \n to match Python's read_text() behavior
        return fs.readFileSync(absPath, 'utf-8').replace(/\r\n/g, '\n');
    } catch {
        return null;
    }
}

/**
 * Walk workspace collecting source files, respecting exclusion rules.
 */
function walkSourceFiles(
    root: string,
    workspace: string,
    exts: Set<string>,
    callback: (absPath: string, relPath: string) => boolean | void,
): void {
    const ws = path.resolve(workspace);

    // Recursive DFS matching Python's os.walk() order: entries sorted
    // alphabetically within each directory, depth-first traversal.
    const walk = (dir: string): boolean => {
        let entries: fs.Dirent[];
        try {
            entries = fs.readdirSync(dir, { withFileTypes: true });
        } catch { return false; }

        // Sort alphabetically to match os.walk() behavior
        entries.sort((a, b) => a.name.localeCompare(b.name));

        // Process files first (same directory), then recurse into subdirs
        for (const entry of entries) {
            if (entry.isFile()) {
                const ext = path.extname(entry.name).toLowerCase();
                if (!exts.has(ext)) { continue; }
                const absPath = path.join(dir, entry.name);
                try {
                    if (fs.statSync(absPath).size > MAX_FILE_SIZE) { continue; }
                } catch { continue; }
                const relPath = path.relative(ws, absPath);
                if (isExcluded(relPath.split(path.sep))) { continue; }
                if (callback(absPath, relPath) === false) { return true; }
            }
        }
        for (const entry of entries) {
            if (entry.isDirectory() && !EXCLUDED_DIRS.has(entry.name)) {
                if (walk(path.join(dir, entry.name))) { return true; }
            }
        }
        return false;
    };

    walk(path.resolve(root));
}

// =========================================================================
// Tool 1: get_dependencies
// =========================================================================

// Import patterns for each language
const IMPORT_PATTERNS: Record<string, RegExp[]> = {
    python: [
        /^\s*from\s+([\w.]+)\s+import/,
        /^\s*import\s+([\w.]+)/,
    ],
    javascript: [
        /(?:import|export)\s+.*?from\s+['"]([^'"]+)['"]/,
        /require\(\s*['"]([^'"]+)['"]\s*\)/,
    ],
    typescript: [
        /(?:import|export)\s+.*?from\s+['"]([^'"]+)['"]/,
        /require\(\s*['"]([^'"]+)['"]\s*\)/,
    ],
    java: [
        /^\s*import\s+([\w.]+)/,
    ],
    go: [
        /^\s*"([^"]+)"/,  // inside import block
    ],
    rust: [
        /^\s*(?:pub\s+)?use\s+([\w:]+)/,
        /^\s*(?:pub\s+)?mod\s+(\w+)/,
    ],
};

/**
 * Parse imports from file content and resolve them to workspace-relative paths.
 */
function parseImports(
    content: string,
    filePath: string,
    lang: string,
    workspace: string,
): Array<{ file_path: string; symbols: string[]; weight: number }> {
    const patterns = IMPORT_PATTERNS[lang] || IMPORT_PATTERNS.python;
    const lines = content.split('\n');
    const imports = new Map<string, { symbols: string[]; weight: number }>();

    for (const line of lines) {
        for (const pat of patterns) {
            const m = line.match(pat);
            if (!m) { continue; }
            const raw = m[1];
            const resolved = resolveImportPath(raw, filePath, lang, workspace);
            if (resolved) {
                const existing = imports.get(resolved);
                if (existing) {
                    existing.weight++;
                } else {
                    imports.set(resolved, { symbols: [raw], weight: 1 });
                }
            }
        }
    }

    return Array.from(imports.entries())
        .map(([file_path, info]) => ({ file_path, ...info }))
        .sort((a, b) => b.weight - a.weight);
}

/**
 * Try to resolve an import specifier to a workspace-relative file path.
 */
function resolveImportPath(
    specifier: string,
    fromFile: string,
    lang: string,
    workspace: string,
): string | null {
    const ws = path.resolve(workspace);

    if (lang === 'python') {
        // Convert dot-separated module path to file path
        const parts = specifier.split('.');
        const candidates = [
            parts.join('/') + '.py',
            parts.join('/') + '/__init__.py',
        ];
        for (const c of candidates) {
            const abs = path.join(ws, c);
            if (fs.existsSync(abs)) { return c; }
        }
        return null;
    }

    if (lang === 'javascript' || lang === 'typescript') {
        if (specifier.startsWith('.')) {
            const dir = path.dirname(path.resolve(ws, fromFile));
            const extensions = ['.ts', '.tsx', '.js', '.jsx', '.mjs', ''];
            for (const ext of extensions) {
                const abs = path.resolve(dir, specifier + ext);
                if (fs.existsSync(abs) && fs.statSync(abs).isFile()) {
                    return path.relative(ws, abs);
                }
                // index file
                const indexAbs = path.join(path.resolve(dir, specifier), `index${ext || '.ts'}`);
                if (fs.existsSync(indexAbs)) {
                    return path.relative(ws, indexAbs);
                }
            }
        }
        return null; // external package
    }

    if (lang === 'java') {
        // com.foo.Bar → com/foo/Bar.java (best effort)
        const parts = specifier.split('.');
        const candidate = parts.join('/') + '.java';
        // Search in common Java source roots
        for (const root of ['src/main/java', 'src', '']) {
            const abs = path.join(ws, root, candidate);
            if (fs.existsSync(abs)) {
                return path.relative(ws, abs);
            }
        }
        return null;
    }

    return null;
}

/**
 * Find files that a given file imports or depends on.
 *
 * @param workspace - Path to the workspace root.
 * @param params - Tool parameters containing the relative file path.
 * @returns ToolResult with an array of relative paths this file imports.
 */
export function get_dependencies(
    workspace: string,
    params: { file_path: string },
): ToolResult {
    const filePath = params.file_path;
    if (!filePath) {
        return { success: false, data: null, error: 'get_dependencies requires file_path' };
    }

    let absPath: string;
    try {
        absPath = resolvePath(workspace, filePath);
    } catch (e: unknown) {
        return { success: false, data: null, error: e instanceof Error ? e.message : String(e) };
    }

    const content = readFileText(absPath);
    if (content === null) {
        return { success: false, data: null, error: `File not found or too large: ${filePath}` };
    }

    const lang = detectLanguage(filePath);
    if (!lang) {
        return { success: true, data: [] };
    }

    const deps = parseImports(content, filePath, lang, workspace);
    return { success: true, data: deps };
}

// =========================================================================
// Tool 2: get_dependents
// =========================================================================

/**
 * Find files that import or depend on a given file.
 *
 * @param workspace - Path to the workspace root.
 * @param params - Tool parameters containing the relative file path.
 * @returns ToolResult with an array of relative paths that import this file.
 */
export function get_dependents(
    workspace: string,
    params: { file_path: string },
): ToolResult {
    const filePath = params.file_path;
    if (!filePath) {
        return { success: false, data: null, error: 'get_dependents requires file_path' };
    }

    const ws = path.resolve(workspace);
    // Build a list of patterns that would match imports of this file
    const baseName = path.basename(filePath, path.extname(filePath));
    const dirName = path.dirname(filePath);
    const escapedPath = filePath.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const escapedBase = baseName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

    // Python: from app.module import ... → module matches
    // JS/TS: from './module' → module matches
    const searchPatterns = [
        new RegExp(`(?:from|import|require).*${escapedBase}`, 'i'),
    ];

    const dependents = new Map<string, { symbols: string[]; weight: number }>();

    walkSourceFiles(ws, workspace, SOURCE_EXTS, (absPath, relPath) => {
        if (relPath === filePath) { return; } // skip self
        const content = readFileText(absPath);
        if (!content) { return; }

        const lang = detectLanguage(relPath);
        if (!lang) { return; }

        // Parse imports and check if any resolve to our target file
        const imports = parseImports(content, relPath, lang, workspace);
        for (const imp of imports) {
            if (imp.file_path === filePath) {
                dependents.set(relPath, {
                    symbols: imp.symbols,
                    weight: imp.weight,
                });
            }
        }
    });

    const result = Array.from(dependents.entries())
        .map(([file_path, info]) => ({ file_path, ...info }))
        .sort((a, b) => b.weight - a.weight);

    return { success: true, data: result };
}

// =========================================================================
// Tool 3: test_outline
// =========================================================================

interface TestOutlineEntry {
    name: string;
    kind: string;
    line_number: number;
    end_line: number;
    mocks: string[];
    assertions: string[];
    fixtures: string[];
}

// Python patterns
const PY_TEST_DEF = /^(\s*)(?:async\s+)?def\s+(test_\w+)\s*\(/;
const PY_CLASS_DEF = /^(\s*)class\s+(Test\w+)/;
const PY_MOCK_PATTERNS = [
    /@(?:mock\.)?patch\(['"](.+?)['"]\s*[),]/,
    /@(?:mock\.)?patch\.object\(\s*(\w+\s*,\s*['"]?\w+)/,
    /mocker\.(?:patch|spy)\(['"](.+?)['"]\)/,
    /monkeypatch\.setattr\((.+?),/,
    /(\w+)\s*=\s*(?:Mock|MagicMock|AsyncMock)\(/,
];
const PY_ASSERT_RE = /(assert\s+.{0,80}|self\.assert\w+\(.{0,60}|pytest\.raises\(.{0,60}\))/;
const PY_FIXTURE_RE = /def\s+test_\w+\(([^)]*)\)/;

// JS/TS patterns
const JS_MOCK_PATTERNS = [
    /jest\.(?:fn|mock|spyOn)\((.{0,60}?)\)/,
    /vi\.(?:fn|mock|spyOn)\((.{0,60}?)\)/,
    /sinon\.(?:stub|spy|mock)\((.{0,60}?)\)/,
];
const JS_ASSERT_RE = /(expect\(.{0,60}\)[\s\S]{0,5}\.[\w.]+\(.{0,60}?\))/;

// Java patterns
const JAVA_TEST_RE = /^\s*@(?:Test|ParameterizedTest|RepeatedTest)\b/;
const JAVA_METHOD_RE = /^\s*(?:public|private|protected)?\s*(?:static\s+)?(?:void|[\w<>\[\],\s]+?)\s+(\w+)\s*\(/;
const JAVA_MOCK_RE = [
    /@Mock\b\s+.*?(\w+)\s*;/,
    /Mockito\.(?:mock|spy|when)\((.{0,60}?)\)/,
    /@InjectMocks\b\s+.*?(\w+)\s*;/,
];

// Go patterns
const GO_TEST_RE = /^func\s+(Test\w+|Benchmark\w+)\s*\(/;
const GO_ASSERT_RE = /(t\.(?:Error|Fatal|Log|Run|Helper|Skip|Assert|Equal|Require)\w*\(.{0,60}?\))/;

// Rust patterns
const RUST_TEST_RE = /^\s*#\[test\]/;
const RUST_FN_RE = /^\s*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)\s*\(/;
const RUST_ASSERT_RE = /(assert(?:_eq|_ne|_matches)?!\(.{0,80}?\))/;

function pyTestOutline(lines: string[]): TestOutlineEntry[] {
    const entries: TestOutlineEntry[] = [];
    const defs: Array<{ name: string; kind: string; line: number; indent: number; endLine: number }> = [];

    // First pass: find test classes and test functions
    for (let i = 0; i < lines.length; i++) {
        const cm = PY_CLASS_DEF.exec(lines[i]);
        if (cm) {
            defs.push({ name: cm[2], kind: 'test_class', line: i + 1, indent: cm[1].length, endLine: 0 });
            continue;
        }
        const fm = PY_TEST_DEF.exec(lines[i]);
        if (fm) {
            defs.push({ name: fm[2], kind: 'test_function', line: i + 1, indent: fm[1].length, endLine: 0 });
        }
    }

    // Compute end_line
    for (let idx = 0; idx < defs.length; idx++) {
        let end = lines.length;
        for (let nxt = idx + 1; nxt < defs.length; nxt++) {
            if (defs[nxt].indent <= defs[idx].indent) {
                end = defs[nxt].line - 1;
                break;
            }
        }
        defs[idx].endLine = end;
    }

    for (const d of defs) {
        // Include decorator lines above
        let decoratorStart = d.line - 1; // 0-based
        for (let k = d.line - 2; k >= 0; k--) {
            const stripped = lines[k].trim();
            if (stripped.startsWith('@')) { decoratorStart = k; }
            else if (stripped === '' || stripped.startsWith('#')) { continue; }
            else { break; }
        }

        const bodyLines = lines.slice(decoratorStart, d.endLine);
        const bodyText = bodyLines.join('\n');

        // Mocks
        const mocks: string[] = [];
        for (const pat of PY_MOCK_PATTERNS) {
            const regex = new RegExp(pat.source, 'g');
            let m: RegExpExecArray | null;
            while ((m = regex.exec(bodyText)) && mocks.length < 10) {
                mocks.push(m[1].trim().slice(0, 80));
            }
        }

        // Assertions
        const assertions: string[] = [];
        const assertRegex = new RegExp(PY_ASSERT_RE.source, 'g');
        let am: RegExpExecArray | null;
        while ((am = assertRegex.exec(bodyText)) && assertions.length < 10) {
            assertions.push(am[1].trim().slice(0, 80));
        }

        // Fixtures
        const fixtures: string[] = [];
        if (d.kind === 'test_function') {
            const defLine = lines[d.line - 1] || '';
            const fm = PY_FIXTURE_RE.exec(defLine);
            if (fm && fm[1]) {
                for (const p of fm[1].split(',')) {
                    const param = p.trim().split(':')[0].split('=')[0].trim();
                    if (param && param !== 'self') { fixtures.push(param); }
                }
            }
        }

        // Prefix class name for methods
        let name = d.name;
        if (d.kind === 'test_function' && d.indent > 0) {
            for (let prev = defs.indexOf(d) - 1; prev >= 0; prev--) {
                if (defs[prev].kind === 'test_class' && defs[prev].indent < d.indent) {
                    name = `${defs[prev].name}::${d.name}`;
                    break;
                }
            }
        }

        entries.push({ name, kind: d.kind, line_number: d.line, end_line: d.endLine, mocks, assertions, fixtures });
    }

    return entries;
}

function jsTestOutline(lines: string[]): TestOutlineEntry[] {
    const entries: TestOutlineEntry[] = [];
    const describeRe = /(?:describe)\s*\(\s*['"`](.+?)['"`]/;
    const testRe = /(?:test|it)\s*\(\s*['"`](.+?)['"`]/;
    const describeStack: string[] = [];
    const describeDepths: number[] = [];
    let braceDepth = 0;

    for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        braceDepth += (line.match(/{/g) || []).length - (line.match(/}/g) || []).length;

        while (describeDepths.length > 0 && braceDepth <= describeDepths[describeDepths.length - 1]) {
            describeStack.pop();
            describeDepths.pop();
        }

        const dm = describeRe.exec(line);
        if (dm) {
            const name = describeStack.length > 0
                ? [...describeStack, dm[1]].join(' > ')
                : dm[1];
            entries.push({ name, kind: 'describe_block', line_number: i + 1, end_line: 0, mocks: [], assertions: [], fixtures: [] });
            describeStack.push(dm[1]);
            describeDepths.push(braceDepth - 1);
            continue;
        }

        const tm = testRe.exec(line);
        if (tm) {
            const name = describeStack.length > 0
                ? [...describeStack, tm[1]].join(' > ')
                : tm[1];

            // Scan ahead for mocks and assertions
            const mocks: string[] = [];
            const assertions: string[] = [];
            let innerBrace = 0;
            let started = false;
            for (let j = i; j < Math.min(i + 100, lines.length); j++) {
                const tl = lines[j];
                innerBrace += (tl.match(/{/g) || []).length - (tl.match(/}/g) || []).length;
                if (tl.includes('{')) { started = true; }
                if (started && innerBrace <= 0) { break; }
                for (const mp of JS_MOCK_PATTERNS) {
                    const mm = mp.exec(tl);
                    if (mm && mocks.length < 10) { mocks.push(mm[1].trim().slice(0, 60)); }
                }
                const aMatch = JS_ASSERT_RE.exec(tl);
                if (aMatch && assertions.length < 10) { assertions.push(aMatch[1].trim().slice(0, 80)); }
            }

            entries.push({ name, kind: 'test_function', line_number: i + 1, end_line: 0, mocks, assertions, fixtures: [] });
        }
    }

    return entries;
}

function javaTestOutline(lines: string[]): TestOutlineEntry[] {
    const entries: TestOutlineEntry[] = [];
    let currentClass = '';
    let nextIsTest = false;

    for (let i = 0; i < lines.length; i++) {
        const line = lines[i];

        // Detect test class
        const classMatch = /^\s*(?:public\s+)?class\s+(\w+)/.exec(line);
        if (classMatch) { currentClass = classMatch[1]; }

        if (JAVA_TEST_RE.test(line)) {
            nextIsTest = true;
            continue;
        }

        if (nextIsTest) {
            const mm = JAVA_METHOD_RE.exec(line);
            if (mm) {
                const name = currentClass ? `${currentClass}::${mm[1]}` : mm[1];

                // Scan body for mocks and assertions
                const mocks: string[] = [];
                const assertions: string[] = [];
                let braceCount = 0;
                let started = false;
                for (let j = i; j < Math.min(i + 100, lines.length); j++) {
                    const tl = lines[j];
                    braceCount += (tl.match(/{/g) || []).length - (tl.match(/}/g) || []).length;
                    if (tl.includes('{')) { started = true; }
                    if (started && braceCount <= 0) { break; }
                    for (const mp of JAVA_MOCK_RE) {
                        const mm2 = mp.exec(tl);
                        if (mm2 && mocks.length < 10) { mocks.push(mm2[1].trim().slice(0, 60)); }
                    }
                    if (/assert\w*\(/.test(tl) && assertions.length < 10) {
                        assertions.push(tl.trim().slice(0, 80));
                    }
                }

                entries.push({ name, kind: 'test_function', line_number: i + 1, end_line: 0, mocks, assertions, fixtures: [] });
            }
            nextIsTest = false;
        }
    }

    return entries;
}

function goTestOutline(lines: string[]): TestOutlineEntry[] {
    const entries: TestOutlineEntry[] = [];
    for (let i = 0; i < lines.length; i++) {
        const m = GO_TEST_RE.exec(lines[i]);
        if (!m) { continue; }
        const name = m[1];
        const kind = name.startsWith('Benchmark') ? 'benchmark' : 'test_function';

        const assertions: string[] = [];
        let braceCount = 0;
        let started = false;
        for (let j = i; j < Math.min(i + 100, lines.length); j++) {
            const tl = lines[j];
            braceCount += (tl.match(/{/g) || []).length - (tl.match(/}/g) || []).length;
            if (tl.includes('{')) { started = true; }
            if (started && braceCount <= 0) { break; }
            const am = GO_ASSERT_RE.exec(tl);
            if (am && assertions.length < 10) { assertions.push(am[1].trim().slice(0, 80)); }
        }

        entries.push({ name, kind, line_number: i + 1, end_line: 0, mocks: [], assertions, fixtures: [] });
    }
    return entries;
}

function rustTestOutline(lines: string[]): TestOutlineEntry[] {
    const entries: TestOutlineEntry[] = [];
    for (let i = 0; i < lines.length; i++) {
        if (!RUST_TEST_RE.test(lines[i])) { continue; }
        // Look for fn on next few lines
        for (let j = i + 1; j < Math.min(i + 5, lines.length); j++) {
            const fm = RUST_FN_RE.exec(lines[j]);
            if (fm) {
                const assertions: string[] = [];
                let braceCount = 0;
                let started = false;
                for (let k = j; k < Math.min(j + 100, lines.length); k++) {
                    const tl = lines[k];
                    braceCount += (tl.match(/{/g) || []).length - (tl.match(/}/g) || []).length;
                    if (tl.includes('{')) { started = true; }
                    if (started && braceCount <= 0) { break; }
                    const am = RUST_ASSERT_RE.exec(tl);
                    if (am && assertions.length < 10) { assertions.push(am[1].trim().slice(0, 80)); }
                }
                entries.push({ name: fm[1], kind: 'test_function', line_number: j + 1, end_line: 0, mocks: [], assertions, fixtures: [] });
                break;
            }
        }
    }
    return entries;
}

/**
 * Extract test structure from a test file (classes, functions, mocks, assertions).
 *
 * @param workspace - Path to the workspace root.
 * @param params - Tool parameters containing the relative path to a test file.
 * @returns ToolResult with structured test metadata (suites, cases, mock usage).
 */
export function test_outline(
    workspace: string,
    params: { path: string },
): ToolResult {
    const filePath = params.path;
    if (!filePath) {
        return { success: false, data: null, error: 'test_outline requires a path' };
    }

    let absPath: string;
    try {
        absPath = resolvePath(workspace, filePath);
    } catch (e: unknown) {
        return { success: false, data: null, error: e instanceof Error ? e.message : String(e) };
    }

    const content = readFileText(absPath);
    if (content === null) {
        return { success: false, data: null, error: `File not found: ${filePath}` };
    }

    const lines = content.split('\n');
    const lang = detectLanguage(filePath);

    let entries: TestOutlineEntry[];
    switch (lang) {
        case 'javascript':
        case 'typescript':
            entries = jsTestOutline(lines);
            break;
        case 'java':
            entries = javaTestOutline(lines);
            break;
        case 'go':
            entries = goTestOutline(lines);
            break;
        case 'rust':
            entries = rustTestOutline(lines);
            break;
        default:
            entries = pyTestOutline(lines);
    }

    return { success: true, data: entries };
}

// =========================================================================
// Tool 4: compressed_view
// =========================================================================

interface SymbolInfo {
    name: string;
    kind: string;
    indent: number;
    start_line: number;
    end_line: number;
    signature: string;
    parent: string | null;
}

const SIDE_EFFECT_PATTERNS: Record<string, string[]> = {
    'db write': [
        'session.add', 'session.commit', '.save()', '.create(',
        '.update(', '.delete(', 'bulk_create', '.objects.create',
        'INSERT', 'UPDATE', 'db.add', 'db.flush', 'db.execute',
    ],
    'http call': [
        'requests.', 'httpx.', 'aiohttp.', 'fetch(',
        'urllib', 'ClientSession',
    ],
    'event publish': [
        'publish(', 'emit(', 'send_event(', 'dispatch(',
        'notify(', 'event_bus.', 'broker.',
    ],
    'file write': [
        '.write(', 'mkdir(', 'shutil.', 'copyfile',
    ],
    'cache write': [
        'cache.set', 'redis.', 'memcached.', '.cache(',
    ],
};

const CALLEE_NOISE = new Set([
    'if', 'for', 'while', 'return', 'print', 'len', 'str', 'int', 'float',
    'bool', 'list', 'dict', 'set', 'tuple', 'range', 'super', 'isinstance',
    'hasattr', 'getattr', 'setattr', 'type', 'None', 'require', 'console',
]);

function extractSymbolsRich(lines: string[], lang: string | null): SymbolInfo[] {
    let classRe: RegExp;
    let funcRe: RegExp;

    switch (lang) {
        case 'javascript':
        case 'typescript':
            classRe = /^(\s*)(?:export\s+)?class\s+(\w+)/;
            funcRe = /^(\s*)(?:export\s+)?(?:async\s+)?(?:function\s+)?(\w+)\s*\(([^)]*)\)/;
            break;
        case 'java':
            classRe = /^(\s*)(?:public|private|protected|abstract|final|static)?\s*class\s+(\w+)/;
            funcRe = /^(\s*)(?:public|private|protected)?\s*(?:static\s+)?(?:\w[\w<>\[\],\s]*?)\s+(\w+)\s*\(([^)]*)\)/;
            break;
        case 'go':
            classRe = /^(\s*)type\s+(\w+)\s+struct/;
            funcRe = /^(\s*)func\s+(?:\([^)]+\)\s+)?(\w+)\s*\(([^)]*)\)/;
            break;
        case 'rust':
            classRe = /^(\s*)(?:pub\s+)?(?:struct|enum|trait)\s+(\w+)/;
            funcRe = /^(\s*)(?:pub\s+)?(?:async\s+)?fn\s+(\w+)\s*\(([^)]*)\)/;
            break;
        default: // python and fallback
            classRe = /^(\s*)class\s+(\w+)\s*[:(]/;
            funcRe = /^(\s*)(?:async\s+)?def\s+(\w+)\s*\(([^)]*)\)/;
    }

    const symbols: SymbolInfo[] = [];

    for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        const cm = classRe.exec(line);
        if (cm) {
            symbols.push({
                name: cm[2], kind: 'class', indent: cm[1].length,
                start_line: i + 1, end_line: i + 1,
                signature: line.trim(), parent: null,
            });
            continue;
        }
        const fm = funcRe.exec(line);
        if (fm) {
            const name = fm[2];
            // Skip dunder noise
            if (name.startsWith('__') && !['__init__', '__call__', '__enter__', '__exit__'].includes(name)) {
                continue;
            }
            const indent = fm[1].length;
            const kind = indent > 0 ? 'method' : 'function';
            let parent: string | null = null;
            for (let prev = symbols.length - 1; prev >= 0; prev--) {
                if (symbols[prev].kind === 'class' && symbols[prev].indent < indent) {
                    parent = symbols[prev].name;
                    break;
                }
            }
            symbols.push({ name, kind, indent, start_line: i + 1, end_line: i + 1, signature: line.trim(), parent });
        }
    }

    // Compute end_line
    for (let idx = 0; idx < symbols.length; idx++) {
        for (let nxt = idx + 1; nxt < symbols.length; nxt++) {
            if (symbols[nxt].indent <= symbols[idx].indent) {
                symbols[idx].end_line = symbols[nxt].start_line - 1;
                break;
            }
        }
        if (symbols[idx].end_line === symbols[idx].start_line) {
            symbols[idx].end_line = lines.length;
        }
    }

    return symbols;
}

function extractCalleesFromBody(bodyLines: string[]): string[] {
    const callRe = /(?:self\.)?(\w+(?:\.\w+)*)\s*\(/g;
    const seen = new Set<string>();
    const result: string[] = [];

    for (const line of bodyLines) {
        const stripped = line.trim();
        if (stripped.startsWith('#') || stripped.startsWith('//')) { continue; }
        let m: RegExpExecArray | null;
        while ((m = callRe.exec(line))) {
            const name = m[1];
            if (!CALLEE_NOISE.has(name) && !seen.has(name)) {
                seen.add(name);
                result.push(name);
            }
        }
    }

    return result;
}

function detectSideEffects(bodyText: string): string[] {
    const effects: string[] = [];
    for (const [effectType, markers] of Object.entries(SIDE_EFFECT_PATTERNS)) {
        if (markers.some(m => bodyText.includes(m))) {
            effects.push(effectType);
        }
    }
    return effects;
}

function extractRaises(bodyText: string): string[] {
    const seen = new Set<string>();
    const result: string[] = [];
    const raiseRe = /raise\s+(\w+)/g;
    const throwRe = /throw\s+new\s+(\w+)/g;
    for (const re of [raiseRe, throwRe]) {
        let m: RegExpExecArray | null;
        while ((m = re.exec(bodyText))) {
            if (!seen.has(m[1])) {
                seen.add(m[1]);
                result.push(m[1]);
            }
        }
    }
    return result;
}

/**
 * Generate a token-efficient summary of a file (signatures, callees, side effects).
 *
 * Achieves approximately 80% token savings compared to reading the full file source.
 *
 * @param workspace - Path to the workspace root.
 * @param params - Tool parameters: file path and optional focus symbol name.
 * @returns ToolResult with a compressed representation of the file's structure.
 */
export function compressed_view(
    workspace: string,
    params: { file_path?: string; path?: string; focus?: string },
): ToolResult {
    const filePath = params.file_path || params.path;
    if (!filePath) {
        return { success: false, data: null, error: 'compressed_view requires file_path' };
    }

    let absPath: string;
    try {
        absPath = resolvePath(workspace, filePath);
    } catch (e: unknown) {
        return { success: false, data: null, error: e instanceof Error ? e.message : String(e) };
    }

    const content = readFileText(absPath);
    if (content === null) {
        return { success: false, data: null, error: `File not found: ${filePath}` };
    }

    const lines = content.split('\n');
    const totalLines = lines.length;
    const lang = detectLanguage(filePath);

    let symbols = extractSymbolsRich(lines, lang);

    if (params.focus) {
        const focusLower = params.focus.toLowerCase();
        symbols = symbols.filter(s =>
            s.name.toLowerCase().includes(focusLower) ||
            (s.parent && s.parent.toLowerCase().includes(focusLower))
        );
    }

    const relPath = path.relative(path.resolve(workspace), absPath);
    const outputLines: string[] = [`## ${relPath} (${totalLines} lines, ${symbols.length} symbols)`, ''];

    for (const sym of symbols) {
        const indent = sym.kind === 'method' ? '    ' : '';
        outputLines.push(`${indent}${sym.signature}`);

        const bodyStart = sym.start_line - 1;
        const bodyEnd = Math.min(sym.end_line, totalLines);
        const bodyLines = lines.slice(bodyStart, bodyEnd);
        const bodyText = bodyLines.join('\n');

        const callees = extractCalleesFromBody(bodyLines);
        if (callees.length > 0) {
            const shown = callees.slice(0, 8).map(c => `${c}()`);
            if (callees.length > 8) { shown.push(`... +${callees.length - 8} more`); }
            outputLines.push(`${indent}    calls: ${shown.join(', ')}`);
        }

        const effects = detectSideEffects(bodyText);
        if (effects.length > 0) {
            outputLines.push(`${indent}    side_effects: ${effects.join(', ')}`);
        }

        const exceptions = extractRaises(bodyText);
        if (exceptions.length > 0) {
            outputLines.push(`${indent}    raises: ${exceptions.join(', ')}`);
        }

        outputLines.push('');
    }

    return {
        success: true,
        data: {
            content: outputLines.join('\n'),
            path: relPath,
            total_lines: totalLines,
            symbol_count: symbols.length,
        },
    };
}

// =========================================================================
// Tool 5: trace_variable
// =========================================================================

function findAliases(bodyLines: string[], startLine: number, variable: string): Array<{ name: string; line: number; expression: string }> {
    const aliases: Array<{ name: string; line: number; expression: string }> = [];
    const known = new Set([variable]);

    for (let pass = 0; pass < 3; pass++) {
        let foundNew = false;
        for (let offset = 0; offset < bodyLines.length; offset++) {
            const stripped = bodyLines[offset].trim();
            if (stripped.startsWith('#') || stripped.startsWith('//')) { continue; }

            for (const name of Array.from(known)) {
                const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
                const re = new RegExp(`\\b(\\w+)\\s*(?::\\s*\\w[\\w\\[\\], ]*\\s*)?=\\s*\\b${escaped}\\b`);
                const m = re.exec(stripped);
                if (m) {
                    const alias = m[1];
                    if (!known.has(alias) && alias !== 'self' && alias !== 'cls' && alias !== 'this') {
                        known.add(alias);
                        aliases.push({ name: alias, line: startLine + offset, expression: stripped.slice(0, 200) });
                        foundNew = true;
                    }
                }
            }
        }
        if (!foundNew) { break; }
    }

    return aliases;
}

// Sink patterns
const SINK_PATTERNS: Array<[string, RegExp]> = [
    ['orm_filter', /\.(?:filter|filter_by|where|having)\s*\([^)]*\bVAR\b/],
    ['orm_get', /\.(?:get|get_or_404|first_or_404|find|findOne|findUnique)\s*\([^)]*\bVAR\b/],
    ['jpa_query', /\.(?:findBy\w*|getBy\w*|deleteBy\w*)\s*\([^)]*\bVAR\b/],
    ['sql_param', /\.(?:execute|executemany|raw|nativeQuery)\b.*\bVAR\b/],
    ['sql_fstring', /(?:SELECT|INSERT|UPDATE|DELETE|WHERE|SET|VALUES)[^;]*\bVAR\b/i],
    ['http_body', /(?:json|data|body|params)\s*[:=]\s*\{[^}]*\bVAR\b/],
    ['return', /\breturn\b[^;\n]*\bVAR\b/],
    ['log', /(?:logger?|console|log)\.\w+\([^)]*\bVAR\b/],
];

// Source patterns
const SOURCE_PATTERNS: Array<[string, RegExp]> = [
    ['http_request', /(?:request|req)\s*\.\s*(?:json|body|form|args|params|query|data)/],
    ['http_annotation', /@(?:RequestParam|PathVariable|RequestBody|QueryParam|PathParam|Body)\b/],
    ['config', /(?:config|settings|env|os\.environ)\s*[\[.]/],
    ['db_result', /\.(?:fetchone|fetchall|first|one|scalar|all)\s*\(/],
];

function detectSinks(bodyLines: string[], startLine: number, allNames: Set<string>): any[] {
    const sinks: any[] = [];
    const seen = new Set<string>();
    const namesPattern = Array.from(allNames).map(n => n.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|');

    for (let offset = 0; offset < bodyLines.length; offset++) {
        const stripped = bodyLines[offset].trim();
        if (stripped.startsWith('#') || stripped.startsWith('//')) { continue; }

        for (const [kind, template] of SINK_PATTERNS) {
            const patStr = template.source.replace(/VAR/g, `(?:${namesPattern})`);
            const pat = new RegExp(patStr, template.flags);
            const m = pat.exec(stripped);
            if (m) {
                const key = `${kind}:${startLine + offset}`;
                if (!seen.has(key)) {
                    seen.add(key);
                    sinks.push({
                        kind,
                        expression: stripped.slice(0, 200),
                        line: startLine + offset,
                        matched_variable: allNames.values().next().value,
                        confidence: 'high',
                    });
                }
            }
        }
    }

    return sinks;
}

function detectSources(bodyLines: string[], startLine: number, variable: string): any[] {
    const sources: any[] = [];
    const varEsc = variable.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

    for (let offset = 0; offset < bodyLines.length; offset++) {
        const stripped = bodyLines[offset].trim();
        if (!stripped.includes(variable)) { continue; }

        for (const [kind, pat] of SOURCE_PATTERNS) {
            if (pat.test(stripped)) {
                sources.push({
                    kind,
                    expression: stripped.slice(0, 200),
                    line: startLine + offset,
                    confidence: 'medium',
                });
                break;
            }
        }
    }

    return sources;
}

function findForwardFlows(
    bodyLines: string[],
    startLine: number,
    allNames: Set<string>,
): any[] {
    const callRe = /(?<!\bdef\s)(?<!\bclass\s)\b([\w.]+)\s*\(/g;
    const flows: any[] = [];
    const seen = new Set<string>();

    for (let offset = 0; offset < bodyLines.length; offset++) {
        const stripped = bodyLines[offset].trim();
        if (stripped.startsWith('#') || stripped.startsWith('//')) { continue; }

        let callMatch: RegExpExecArray | null;
        while ((callMatch = callRe.exec(stripped))) {
            const funcExpr = callMatch[1];
            const callStart = callMatch.index + callMatch[0].length - 1;

            // Extract arguments inside parens
            const argsStr = extractParenContent(stripped, callStart);
            if (!argsStr) { continue; }

            const argParts = argsStr.split(',');

            for (let argIdx = 0; argIdx < argParts.length; argIdx++) {
                const argText = argParts[argIdx].trim();
                for (const name of allNames) {
                    const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
                    if (!new RegExp(`\\b${escaped}\\b`).test(argText)) { continue; }

                    const kwMatch = /^(\w+)\s*=\s*/.exec(argText);
                    const asParam = kwMatch ? kwMatch[1] : `arg[${argIdx}]`;
                    const funcSimple = funcExpr.split('.').pop() || funcExpr;
                    const dedupKey = `${funcSimple}:${argIdx}:${startLine + offset}`;
                    if (seen.has(dedupKey)) { continue; }
                    seen.add(dedupKey);

                    flows.push({
                        callee_function: funcSimple,
                        full_expression: funcExpr,
                        as_parameter: asParam,
                        arg_expression: argText,
                        call_line: startLine + offset,
                        arg_position: argIdx,
                        confidence: kwMatch ? 'high' : 'medium',
                    });
                    break;
                }
            }
        }
    }

    return flows;
}

function extractParenContent(line: string, openIdx: number): string | null {
    if (line[openIdx] !== '(') { return null; }
    let depth = 1;
    let i = openIdx + 1;
    while (i < line.length && depth > 0) {
        if (line[i] === '(') { depth++; }
        if (line[i] === ')') { depth--; }
        i++;
    }
    if (depth !== 0) { return null; }
    return line.slice(openIdx + 1, i - 1);
}

/** Simple definition finder using regex (no tree-sitter dependency). */
function findFunctionDefs(lines: string[]): Array<{ name: string; kind: string; startLine: number; endLine: number; indent: number }> {
    const defs: Array<{ name: string; kind: string; startLine: number; endLine: number; indent: number }> = [];
    const defRe = /^(\s*)(?:async\s+)?(?:def|function|fn|func)\s+(\w+)\s*\(/;
    const classRe = /^(\s*)class\s+(\w+)/;

    for (let i = 0; i < lines.length; i++) {
        const cm = classRe.exec(lines[i]);
        if (cm) {
            defs.push({ name: cm[2], kind: 'class', startLine: i + 1, endLine: 0, indent: cm[1].length });
            continue;
        }
        const fm = defRe.exec(lines[i]);
        if (fm) {
            defs.push({ name: fm[2], kind: 'function', startLine: i + 1, endLine: 0, indent: fm[1].length });
        }
    }

    // Compute end lines
    for (let idx = 0; idx < defs.length; idx++) {
        let end = lines.length;
        for (let nxt = idx + 1; nxt < defs.length; nxt++) {
            if (defs[nxt].indent <= defs[idx].indent) {
                end = defs[nxt].startLine - 1;
                break;
            }
        }
        defs[idx].endLine = end;
    }

    return defs;
}

/**
 * Trace data flow for a variable in the forward or backward direction.
 *
 * @param workspace - Path to the workspace root.
 * @param params - Tool parameters: variable name, file, optional function scope, and direction.
 * @returns ToolResult with aliases, flows_to, sinks, and sources for the variable.
 */
export function trace_variable(
    workspace: string,
    params: { variable_name: string; file: string; function_name?: string; direction?: string },
): ToolResult {
    const { variable_name, file, function_name, direction = 'forward' } = params;
    if (!variable_name || !file) {
        return { success: false, data: null, error: 'trace_variable requires variable_name and file' };
    }

    let absPath: string;
    try {
        absPath = resolvePath(workspace, file);
    } catch (e: unknown) {
        return { success: false, data: null, error: e instanceof Error ? e.message : String(e) };
    }

    const content = readFileText(absPath);
    if (content === null) {
        return { success: false, data: null, error: `File not found: ${file}` };
    }

    const lines = content.split('\n');
    const defs = findFunctionDefs(lines);

    // Resolve target function
    let targetDef: (typeof defs)[0] | undefined;
    if (function_name) {
        targetDef = defs.find(d => d.name === function_name);
        if (!targetDef) {
            return { success: false, data: null, error: `Function '${function_name}' not found in ${file}` };
        }
    } else {
        const escaped = variable_name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        const varRe = new RegExp(`\\b${escaped}\\b`);
        targetDef = defs.find(d => {
            if (d.kind !== 'function') { return false; }
            const body = lines.slice(d.startLine - 1, d.endLine).join('\n');
            return varRe.test(body);
        });
        if (!targetDef) {
            return { success: false, data: null, error: `No function in ${file} references '${variable_name}'` };
        }
    }

    const startLine = targetDef.startLine;
    const endLine = targetDef.endLine;
    const bodyLines = lines.slice(startLine - 1, endLine);

    const aliases = findAliases(bodyLines, startLine, variable_name);
    const allNames = new Set([variable_name, ...aliases.map(a => a.name)]);

    const ws = path.resolve(workspace);
    const relFile = path.relative(ws, absPath);

    const result: Record<string, any> = {
        variable: variable_name,
        file: relFile,
        function: targetDef.name,
        direction,
        aliases,
        flows_to: [],
        sinks: [],
        flows_from: [],
        sources: [],
    };

    if (direction === 'forward') {
        result.flows_to = findForwardFlows(bodyLines, startLine, allNames);
        result.sinks = detectSinks(bodyLines, startLine, allNames);
    } else {
        result.sources = detectSources(bodyLines, startLine, variable_name);
    }

    return { success: true, data: result };
}

// =========================================================================
// Tool 6: module_summary
// =========================================================================

/** Regex-based symbol extraction for module_summary (class + function definitions). */
function extractModuleSymbols(content: string, lang: string | null): { classes: string[]; functions: string[] } {
    const classes: string[] = [];
    const functions: string[] = [];
    const lines = content.split('\n');

    let classRe: RegExp;
    let funcRe: RegExp;

    switch (lang) {
        case 'javascript':
        case 'typescript':
            classRe = /^(?:export\s+)?class\s+(\w+)/;
            funcRe = /^(?:export\s+)?(?:async\s+)?(?:function\s+)?(\w+)\s*\(/;
            break;
        case 'java':
            classRe = /^\s*(?:public|private|protected|abstract|final|static)?\s*class\s+(\w+)/;
            funcRe = /^\s*(?:public|private|protected)?\s*(?:static\s+)?(?:\w[\w<>\[\],\s]*?)\s+(\w+)\s*\(/;
            break;
        case 'go':
            classRe = /^type\s+(\w+)\s+struct/;
            funcRe = /^func\s+(?:\([^)]+\)\s+)?(\w+)\s*\(/;
            break;
        case 'rust':
            classRe = /^(?:pub\s+)?(?:struct|enum|trait)\s+(\w+)/;
            funcRe = /^(?:pub\s+)?(?:async\s+)?fn\s+(\w+)\s*\(/;
            break;
        default: // python
            classRe = /^class\s+(\w+)\s*[:(]/;
            funcRe = /^(?:async\s+)?def\s+(\w+)\s*\(/;
    }

    for (const line of lines) {
        const stripped = line.trimStart();
        const cm = classRe.exec(stripped);
        if (cm) { classes.push(cm[1]); continue; }
        const fm = funcRe.exec(stripped);
        if (fm) { functions.push(fm[1]); }
    }

    return { classes, functions };
}

/**
 * Generate a high-level module overview with approximately 95% token savings.
 *
 * @param workspace - Path to the workspace root.
 * @param params - Tool parameters: module directory or file path (accepts multiple aliases).
 * @returns ToolResult with a concise summary of the module's exported symbols and purpose.
 */
export async function module_summary(
    workspace: string,
    params: { module_path?: string; path?: string; file_path?: string },
): Promise<ToolResult> {
    const modulePath = params.module_path || params.path || params.file_path;
    if (!modulePath) {
        return { success: false, data: null, error: 'module_summary requires module_path' };
    }

    let absPath: string;
    try {
        absPath = resolvePath(workspace, modulePath);
    } catch (e: unknown) {
        return { success: false, data: null, error: e instanceof Error ? e.message : String(e) };
    }

    let stat: fs.Stats;
    try {
        stat = fs.statSync(absPath);
    } catch {
        return { success: false, data: null, error: `Path not found: ${modulePath}` };
    }

    if (!stat.isDirectory()) {
        return { success: false, data: null, error: `Directory not found: ${modulePath}` };
    }

    const ws = path.resolve(workspace);

    // Collect ALL source files first (matching Python's os.walk behavior),
    // then cap at 100 for processing. This preserves the real file count.
    const allSourceFiles: string[] = [];
    walkSourceFiles(absPath, workspace, MODULE_SUMMARY_EXTS, (_absPath, relPath) => {
        allSourceFiles.push(relPath);
    });

    if (allSourceFiles.length === 0) {
        return {
            success: true,
            data: { content: `## Module: ${modulePath}\nNo source files found.`, file_count: 0, loc: 0 },
        };
    }

    // Cap at 100 for processing (matching Python's source_files[:100]).
    // Walk order is DFS alphabetical per-directory, close to Python's os.walk.
    const sourceFiles = allSourceFiles.slice(0, 100);

    let totalLoc = 0;
    const allClasses: string[] = [];
    const allFunctions: string[] = [];
    const importModules = new Set<string>();

    for (const relFile of sourceFiles) {
        const fileAbsPath = path.join(ws, relFile);
        const content = readFileText(fileAbsPath);
        if (!content) { continue; }

        // Use same counting as Python's splitlines() — ignore trailing newline
        const lines = content.split('\n');
        const lineCount = content.endsWith('\n') ? lines.length - 1 : lines.length;
        totalLoc += lineCount;

        // Use tree-sitter AST extraction when available (matches Python backend).
        // Falls back to regex for unsupported languages or when tree-sitter is not initialized.
        let usedTreeSitter = false;
        if (treeSitter.isInitialized()) {
            try {
                const tsResult = await treeSitter.extractDefinitions(relFile, Buffer.from(content));
                for (const d of tsResult.definitions) {
                    if (d.kind === 'class') { allClasses.push(d.name); }
                    else if (d.kind === 'function' || d.kind === 'method') { allFunctions.push(d.name); }
                }
                usedTreeSitter = true;
            } catch { /* fall through to regex */ }
        }
        if (!usedTreeSitter) {
            const lang = detectLanguage(relFile);
            const syms = extractModuleSymbols(content, lang);
            allClasses.push(...syms.classes);
            allFunctions.push(...syms.functions);
        }

        // Quick import extraction (first 100 lines)
        for (const line of lines.slice(0, 100)) {
            const stripped = line.trim();
            if (stripped.startsWith('from ') || stripped.startsWith('import ')) {
                const parts = stripped.split(/\s+/);
                if (parts.length >= 2) {
                    const mod = parts[1].split('.')[0];
                    if (mod && mod !== '.' && mod !== '..') {
                        importModules.add(mod);
                    }
                }
            }
        }
    }

    // Classify notable symbols
    const services = allClasses.filter(c => c.includes('Service') || c.includes('Manager'));
    const models = allClasses.filter(c =>
        ['Model', 'Schema', 'Entity', 'DTO'].some(kw => c.includes(kw)));
    const controllers = allClasses.filter(c =>
        ['Controller', 'Router', 'Handler', 'View'].some(kw => c.includes(kw)));
    const serviceSet = new Set([...services, ...models, ...controllers]);
    const remainingClasses = allClasses.filter(c => !serviceSet.has(c));

    // Build summary text
    const relModule = path.relative(ws, absPath);
    const outputLines: string[] = [
        `## Module: ${relModule} (${allSourceFiles.length} files, ${totalLoc.toLocaleString()} LOC)`,
        '',
    ];

    if (services.length > 0) { outputLines.push(`Key Services: ${services.slice(0, 15).join(', ')}`); }
    if (models.length > 0) { outputLines.push(`Key Models: ${models.slice(0, 15).join(', ')}`); }
    if (controllers.length > 0) { outputLines.push(`Controllers: ${controllers.slice(0, 15).join(', ')}`); }
    if (remainingClasses.length > 0) { outputLines.push(`Other Classes: ${remainingClasses.slice(0, 15).join(', ')}`); }

    const notableFns = allFunctions.filter(f =>
        !f.startsWith('_') && !['__init__', 'setUp', 'tearDown'].includes(f));
    if (notableFns.length > 0) {
        outputLines.push(`Key Functions (${notableFns.length} total): ${notableFns.slice(0, 20).join(', ')}`);
    }

    if (importModules.size > 0) {
        outputLines.push(`\nExternal Imports: ${Array.from(importModules).sort().slice(0, 20).join(', ')}`);
    }

    // List files (sorted for display, first 30 — matches Python's sorted(source_files)[:30])
    outputLines.push(`\nFiles (${allSourceFiles.length}):`);
    const sortedForDisplay = [...allSourceFiles].sort();
    for (const f of sortedForDisplay.slice(0, 30)) {
        const fileContent = readFileText(path.join(ws, f));
        // Match Python's len(splitlines()): trailing \n does not add a line
        const lines = fileContent ? fileContent.split('\n') : [];
        const loc = fileContent && fileContent.endsWith('\n') ? lines.length - 1 : lines.length;
        outputLines.push(`  ${f} (${loc} lines)`);
    }
    if (allSourceFiles.length > 30) {
        outputLines.push(`  ... and ${allSourceFiles.length - 30} more files`);
    }

    return {
        success: true,
        data: {
            content: outputLines.join('\n'),
            file_count: allSourceFiles.length,
            loc: totalLoc,
        },
    };
}

// =========================================================================
// Tool 7: detect_patterns
// =========================================================================

type PatternEntry = [RegExp, string];

const PATTERN_CATEGORIES: Record<string, PatternEntry[]> = {
    webhook: [
        [/(?:@(?:post|put|delete|patch)mapping\b.*(?:callback|hook|notify|webhook))/i, 'webhook endpoint'],
        [/(?:app\.(?:post|put)\(.*(?:callback|hook|notify|webhook))/i, 'webhook route'],
        [/(?:router\.(?:post|put)\(.*(?:callback|hook|notify|webhook))/i, 'webhook route'],
        [/(?:def\s+\w*(?:callback|hook|webhook|notify)\w*\s*\()/i, 'webhook/callback handler'],
        [/(?:on_?event|event_?handler|subscribe|add_?listener)\s*\(/i, 'event listener'],
    ],
    queue: [
        [/@(?:rabbit|sqs|kafka|jms)listener\b/i, 'queue consumer annotation'],
        [/\b(?:consume|consumer|subscriber|on_message)\s*\(/i, 'queue consumer'],
        [/\b(?:publish|produce|send_message|enqueue)\s*\(/i, 'queue producer'],
        [/(?:kafka|sqs|rabbit|amqp|pubsub|celery|rq)\./i, 'message queue usage'],
        [/@app\.task|@shared_task|@celery\.task/i, 'Celery task'],
    ],
    retry: [
        [/@retry\b|@backoff\b|@retrying\b/i, 'retry decorator'],
        [/\bretry[\s_]*(count|max|limit|attempts)\b/i, 'retry config'],
        [/\b(exponential_?backoff|backoff_?factor|retry_?delay)\b/i, 'backoff config'],
        [/Retrying|tenacity\.retry|urllib3\.util\.retry/i, 'retry library'],
    ],
    lock: [
        [/\b(acquire|release)\s*\(\s*\)/i, 'lock acquire/release'],
        [/\b(Lock|RLock|Semaphore|Mutex|ReentrantLock)\s*\(/i, 'lock creation'],
        [/with\s+\w*lock/i, 'lock context manager'],
        [/synchronized\b/i, 'synchronized block (Java)'],
        [/\b(redis|distributed)[\s_]*lock\b/i, 'distributed lock'],
        [/SELECT\s+.*\s+FOR\s+UPDATE/i, 'SELECT FOR UPDATE'],
    ],
    check_then_act: [
        [/if\s+not\s+.*(?:exists?|find|get)\b.*:\s*$/i, 'check-then-act guard'],
        [/\.get_or_create\b|\.find_or_create\b|\.upsert\b/i, 'atomic alternative (good)'],
    ],
    transaction: [
        [/@transactional\b/i, 'transaction annotation'],
        [/\b(begin|commit|rollback)\s*\(/i, 'transaction boundary'],
        [/with\s+.*(?:transaction|session|atomic)\b/i, 'transaction context'],
        [/(?:connection|session|db)\.(begin|commit|rollback)/i, 'explicit transaction'],
        [/auto_?commit\s*=\s*(True|true|1)/i, 'auto-commit enabled (risky)'],
    ],
    token_lifecycle: [
        [/\b(generate|create|issue)[\s_]*(token|jwt|session)\b/i, 'token creation'],
        [/\b(validate|verify|decode)[\s_]*(token|jwt)\b/i, 'token validation'],
        [/\b(refresh|renew|rotate)[\s_]*(token|jwt|session)\b/i, 'token refresh'],
        [/\b(revoke|invalidate|expire|blacklist)[\s_]*(token|jwt|session)\b/i, 'token revocation'],
    ],
    side_effect_chain: [
        [/\b(send_?email|send_?notification|send_?sms|notify)\s*\(/i, 'notification side effect'],
        [/\b(audit_?log|log_?event|track|emit_?event)\s*\(/i, 'audit/event side effect'],
        [/\b(charge|refund|transfer|debit|credit)\s*\(/i, 'financial side effect'],
        [/\bhttpx?\.(post|put|delete|patch)\b/i, 'outbound HTTP side effect'],
        [/\brequests\.(post|put|delete|patch)\b/i, 'outbound HTTP side effect'],
    ],
};

/**
 * Find architectural patterns (webhook, queue, retry, etc.) in the codebase.
 *
 * @param workspace - Path to the workspace root.
 * @param params - Tool parameters: optional directory scope, pattern categories, and result limit.
 * @returns ToolResult with matched patterns grouped by category and file location.
 */
export function detect_patterns(
    workspace: string,
    params: { path?: string; categories?: string[]; max_results?: number },
): ToolResult {
    const maxResults = params.max_results || 50;
    const ws = path.resolve(workspace);
    let scanRoot: string;

    if (params.path) {
        try {
            scanRoot = resolvePath(workspace, params.path);
        } catch (e: unknown) {
            return { success: false, data: null, error: e instanceof Error ? e.message : String(e) };
        }
    } else {
        scanRoot = ws;
    }

    if (!fs.existsSync(scanRoot)) {
        return { success: false, data: null, error: `Path not found: ${params.path || '.'}` };
    }

    // Filter categories
    let activeCategories = PATTERN_CATEGORIES;
    if (params.categories && params.categories.length > 0) {
        const valid = params.categories.filter(c => c in PATTERN_CATEGORIES);
        if (valid.length === 0) {
            return { success: false, data: null, error: `Unknown categories: ${params.categories}. Valid: ${Object.keys(PATTERN_CATEGORIES).sort().join(', ')}` };
        }
        activeCategories = Object.fromEntries(valid.map(c => [c, PATTERN_CATEGORIES[c]]));
    }

    const resultsByCategory: Record<string, Array<{ file: string; line: number; pattern: string; snippet: string }>> = {};
    let totalMatches = 0;
    let filesScanned = 0;

    const scanFile = (absPath: string, relPath: string): boolean | void => {
        if (totalMatches >= maxResults) { return false; }
        filesScanned++;
        const content = readFileText(absPath);
        if (!content) { return; }
        const lines = content.split('\n');

        for (const [catName, patterns] of Object.entries(activeCategories)) {
            if (totalMatches >= maxResults) { return false; }
            for (let lineNum = 0; lineNum < lines.length; lineNum++) {
                if (totalMatches >= maxResults) { return false; }
                const line = lines[lineNum];
                for (const [pat, desc] of patterns) {
                    if (pat.test(line)) {
                        if (!resultsByCategory[catName]) { resultsByCategory[catName] = []; }
                        resultsByCategory[catName].push({
                            file: relPath,
                            line: lineNum + 1,
                            pattern: desc,
                            snippet: line.trim().slice(0, 200),
                        });
                        totalMatches++;
                        break; // one match per line per category
                    }
                }
            }
        }
    };

    if (fs.statSync(scanRoot).isFile()) {
        const relPath = path.relative(ws, scanRoot);
        scanFile(scanRoot, relPath);
    } else {
        walkSourceFiles(scanRoot, workspace, SCANNABLE_EXTS, scanFile);
    }

    const summary: Record<string, number> = {};
    for (const [cat, matches] of Object.entries(resultsByCategory)) {
        summary[cat] = matches.length;
    }

    return {
        success: true,
        data: {
            summary,
            total_matches: totalMatches,
            categories_scanned: Object.keys(activeCategories).sort(),
            files_scanned: filesScanned,
            matches: resultsByCategory,
        },
        truncated: totalMatches >= maxResults,
    };
}
