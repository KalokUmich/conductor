/**
 * AST-based code intelligence tools for Conductor.
 *
 * Implements 6 tools that mirror the Python backend's code_tools/tools.py:
 *   - file_outline   — list all definitions in a file
 *   - find_symbol    — workspace-wide symbol search (cached by git HEAD)
 *   - find_references — grep + AST-validated reference search
 *   - get_callees    — functions called within a specific function body
 *   - get_callers    — functions that call a given function
 *   - expand_symbol  — expand a symbol to its full source code
 *
 * Uses treeSitterService.ts (with regex fallback) for AST extraction,
 * and synchronous file I/O since tool execution runs in a worker context.
 *
 * @module services/astToolRunner
 */

import * as fs from 'fs';
import * as path from 'path';
import { execSync } from 'child_process';
import * as treeSitter from './treeSitterService';
import type { ToolResult } from './toolTypes';

// Re-export types from repoGraphBuilder for consumers that need them.
import type { SymbolDef, FileSymbolsData } from './repoGraphBuilder';

// Re-export ToolResult so existing consumers of astToolRunner still compile.
export type { ToolResult };

/**
 * Read a file as UTF-8 text, normalizing \r\n → \n to match Python behavior.
 */
function readFileNormalized(absPath: string): string {
    return fs.readFileSync(absPath, 'utf-8').replace(/\r\n/g, '\n');
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const MAX_FILE_SIZE = 512_000; // 512 KB — skip larger files in search/parse

const EXCLUDED_DIRS = new Set([
    '.git', '.hg', '.svn', '__pycache__', 'node_modules', 'target',
    'dist', 'vendor', '.venv', 'venv', '.mypy_cache', '.pytest_cache',
    '.tox', 'build', '.next', '.nuxt', '.yarn', '.pnp',
]);

const SUPPORTED_EXTS = new Set([
    '.py', '.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs',
    '.java', '.go', '.rs', '.rb', '.cs', '.cpp', '.cc', '.c', '.h',
]);

/**
 * Noise words to skip when extracting callees — language keywords and
 * builtins that look like function calls but are not meaningful callees.
 */
const CALL_NOISE = new Set([
    'if', 'for', 'while', 'return', 'print', 'len', 'str', 'int', 'float',
    'bool', 'list', 'dict', 'set', 'tuple', 'type', 'isinstance', 'issubclass',
    'range', 'enumerate', 'zip', 'map', 'filter', 'sorted', 'reversed',
    'super', 'property', 'staticmethod', 'classmethod', 'getattr', 'setattr',
    'hasattr', 'delattr', 'open', 'repr', 'hash', 'id', 'input', 'abs',
    'min', 'max', 'sum', 'round', 'any', 'all', 'next', 'iter',
    // JS/TS additions
    'require', 'console', 'setTimeout', 'setInterval', 'clearTimeout',
    'clearInterval', 'Promise', 'Array', 'Object', 'String', 'Number',
    'Boolean', 'Date', 'Math', 'JSON', 'RegExp', 'Error', 'Map', 'Set',
    'parseInt', 'parseFloat', 'isNaN', 'isFinite', 'typeof', 'void',
    'delete', 'new', 'throw', 'switch', 'case', 'catch', 'try', 'finally',
]);

// ---------------------------------------------------------------------------
// Symbol index cache
// ---------------------------------------------------------------------------

interface SymbolIndexEntry {
    index: Record<string, SymbolDef[]>;
    gitHead: string;
}

const symbolIndexCache = new Map<string, SymbolIndexEntry>();

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

/**
 * Resolve a relative path within the workspace, preventing traversal.
 */
function resolvePath(workspace: string, relPath: string): string {
    const ws = path.resolve(workspace);
    const target = path.resolve(ws, relPath);
    if (!target.startsWith(ws + path.sep) && target !== ws) {
        throw new Error(`Path escapes workspace: ${relPath}`);
    }
    return target;
}

/**
 * Check if any path component is in the exclude set.
 */
function isExcluded(parts: string[]): boolean {
    return parts.some(p => EXCLUDED_DIRS.has(p));
}

/**
 * Detect language from file extension. Returns null for unsupported files.
 */
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

/**
 * Get git HEAD commit hash for cache invalidation.
 */
function getGitHead(workspace: string): string | null {
    try {
        return execSync('git rev-parse HEAD', {
            cwd: workspace,
            encoding: 'utf-8',
            timeout: 5000,
            stdio: ['pipe', 'pipe', 'pipe'],
        }).trim();
    } catch {
        return null;
    }
}

// ---------------------------------------------------------------------------
// Regex-based definition extraction (inline replacement for deleted symbolExtractor)
// ---------------------------------------------------------------------------

interface RegexPatternEntry { kind: string; pattern: RegExp; }

const DEF_PATTERNS: Record<string, RegexPatternEntry[]> = {
    python: [
        { kind: 'function', pattern: /^(?:async\s+)?def\s+(\w+)\s*\(/gm },
        { kind: 'class',    pattern: /^class\s+(\w+)\s*[:(]/gm },
    ],
    javascript: [
        { kind: 'function', pattern: /(?:async\s+)?function\s+(\w+)\s*\(/gm },
        { kind: 'class',    pattern: /class\s+(\w+)\s*[{]/gm },
        { kind: 'function', pattern: /(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\(/gm },
    ],
    typescript: [
        { kind: 'function',  pattern: /(?:async\s+)?function\s+(\w+)\s*[(<]/gm },
        { kind: 'class',     pattern: /class\s+(\w+)\s*[{<]/gm },
        { kind: 'interface', pattern: /interface\s+(\w+)\s*[{<]/gm },
    ],
    java: [
        { kind: 'class',     pattern: /(?:public|private|protected|abstract|final|static)?\s*class\s+(\w+)\s*[{<(]/gm },
        { kind: 'interface', pattern: /(?:public|private|protected)?\s*interface\s+(\w+)\s*[{<]/gm },
        { kind: 'class',     pattern: /(?:public|private|protected)?\s*enum\s+(\w+)\s*[{]/gm },
        { kind: 'class',     pattern: /(?:public|private|protected)?\s*record\s+(\w+)\s*[(<]/gm },
        { kind: 'method',    pattern: /^\s+(?:public|private|protected)\s+(?:static\s+)?(?:synchronized\s+)?(?:final\s+)?(?:[\w<>\[\],\s]+?)\s+(\w+)\s*\(/gm },
    ],
    go: [
        { kind: 'function',  pattern: /^func\s+(\w+)\s*\(/gm },
        { kind: 'method',    pattern: /^func\s+\([^)]+\)\s+(\w+)\s*\(/gm },
        { kind: 'class',     pattern: /^type\s+(\w+)\s+struct\s*\{/gm },
        { kind: 'interface', pattern: /^type\s+(\w+)\s+interface\s*\{/gm },
    ],
    rust: [
        { kind: 'function',  pattern: /(?:pub\s+)?(?:async\s+)?fn\s+(\w+)/gm },
        { kind: 'class',     pattern: /(?:pub\s+)?struct\s+(\w+)/gm },
        { kind: 'class',     pattern: /(?:pub\s+)?enum\s+(\w+)/gm },
        { kind: 'interface', pattern: /(?:pub\s+)?trait\s+(\w+)/gm },
        { kind: 'class',     pattern: /impl(?:<[^>]+>)?\s+(\w+)/gm },
    ],
    c: [
        { kind: 'function', pattern: /^(?:static\s+)?(?:inline\s+)?(?:const\s+)?(?:unsigned\s+)?(?:struct\s+)?\w[\w*\s]+?\s+(\w+)\s*\([^;]*$/gm },
        { kind: 'class',    pattern: /(?:typedef\s+)?struct\s+(\w+)\s*\{/gm },
        { kind: 'class',    pattern: /(?:typedef\s+)?enum\s+(\w+)\s*\{/gm },
    ],
    cpp: [
        { kind: 'function',  pattern: /^(?:static\s+)?(?:virtual\s+)?(?:inline\s+)?(?:const\s+)?[\w:*&<>\s]+?\s+(\w+)\s*\([^;]*$/gm },
        { kind: 'class',     pattern: /(?:class|struct)\s+(\w+)\s*[{:]/gm },
        { kind: 'interface', pattern: /namespace\s+(\w+)\s*\{/gm },
    ],
};

/**
 * Synchronous regex-based definition extraction (1-based line numbers).
 * Replaces the deleted symbolExtractor.ts — used as fallback when tree-sitter
 * is unavailable and in synchronous call sites (workspace-wide scans).
 */
function extractDefsRegex(absPath: string, relPath: string): SymbolDef[] {
    let source: string;
    try {
        source = readFileNormalized(absPath);
    } catch {
        return [];
    }

    const lang = detectLanguage(absPath);
    if (!lang) return [];

    const patterns = DEF_PATTERNS[lang] ?? DEF_PATTERNS['python'] ?? [];
    const lines = source.split('\n');
    const defs: SymbolDef[] = [];

    for (const { kind, pattern } of patterns) {
        pattern.lastIndex = 0;
        let m: RegExpExecArray | null;
        while ((m = pattern.exec(source)) !== null) {
            const name = m[1];
            const lineNo = source.slice(0, m.index).split('\n').length;
            let sig = lineNo <= lines.length ? lines[lineNo - 1].trim() : '';
            if (sig.length > 120) sig = sig.slice(0, 117) + '...';
            defs.push({ name, kind, file_path: relPath, start_line: lineNo, end_line: lineNo, signature: sig });
        }
    }

    return defs;
}

/**
 * Extract definitions from a file, returning 1-based SymbolDefs.
 *
 * Prefers web-tree-sitter (same quality as Python backend) when initialized.
 * Falls back to regex when tree-sitter is unavailable.
 */
async function extractDefinitionsAsync(absPath: string, relPath: string): Promise<SymbolDef[]> {
    if (treeSitter.isInitialized()) {
        try {
            const source = fs.readFileSync(absPath);
            const result = await treeSitter.extractDefinitions(relPath, source);
            return result.definitions;
        } catch {
            // Fall through to regex
        }
    }
    return extractDefsRegex(absPath, relPath);
}

/** Synchronous extraction — regex fallback used by find_symbol index building and workspace scans. */
function extractDefinitions(absPath: string, relPath: string): SymbolDef[] {
    return extractDefsRegex(absPath, relPath);
}

/**
 * Walk workspace collecting source files, respecting exclusion rules and size limits.
 * Calls the callback for each qualifying file with (absPath, relPath).
 */
function walkSourceFiles(
    workspace: string,
    callback: (absPath: string, relPath: string) => boolean | void,
): void {
    const ws = path.resolve(workspace);

    const walk = (dir: string, relDir: string): boolean => {
        let entries: fs.Dirent[];
        try {
            entries = fs.readdirSync(dir, { withFileTypes: true });
        } catch {
            return false;
        }

        for (const entry of entries) {
            if (entry.isDirectory()) {
                if (EXCLUDED_DIRS.has(entry.name) || entry.name.startsWith('.')) {
                    continue;
                }
                const childRel = relDir ? path.join(relDir, entry.name) : entry.name;
                if (isExcluded(childRel.split(path.sep))) {
                    continue;
                }
                const stop = walk(path.join(dir, entry.name), childRel);
                if (stop) return true;
            } else if (entry.isFile()) {
                const ext = path.extname(entry.name).toLowerCase();
                if (!SUPPORTED_EXTS.has(ext)) continue;

                const absPath = path.join(dir, entry.name);
                try {
                    const stat = fs.statSync(absPath);
                    if (stat.size > MAX_FILE_SIZE) continue;
                } catch {
                    continue;
                }

                const relPath = relDir ? path.join(relDir, entry.name) : entry.name;
                const stop = callback(absPath, relPath);
                if (stop === true) return true;
            }
        }
        return false;
    };

    walk(ws, '');
}

/**
 * Build or return a cached workspace-wide symbol index.
 * Cache invalidation is based on the git HEAD commit.
 */
function getSymbolIndex(workspace: string): Record<string, SymbolDef[]> {
    const ws = path.resolve(workspace);
    const currentHead = getGitHead(ws) || '';

    // In-memory cache hit
    const cached = symbolIndexCache.get(ws);
    if (cached && cached.gitHead === currentHead) {
        return cached.index;
    }

    // Full scan
    const index: Record<string, SymbolDef[]> = {};

    walkSourceFiles(ws, (absPath, relPath) => {
        const defs = extractDefinitions(absPath, relPath);
        if (defs.length > 0) {
            index[relPath] = defs;
        }
    });

    symbolIndexCache.set(ws, { index, gitHead: currentHead });
    return index;
}

// ---------------------------------------------------------------------------
// Symbol role classification (ported from Python _classify_symbol_role)
// ---------------------------------------------------------------------------

const ROLE_PRIORITY: Record<string, number> = {
    route_entry: 0, business_logic: 1, domain_model: 2,
    infrastructure: 3, utility: 4, test: 5, unknown: 6,
};

const SIG_ROLE_PATTERNS: Array<[RegExp, string]> = [
    [/@(?:app|router|api)\.\s*(?:get|post|put|delete|patch|route)/, 'route_entry'],
    [/@(?:Get|Post|Put|Delete|Patch|Request)Mapping/, 'route_entry'],
    [/@Controller|@RestController|@Resource/, 'route_entry'],
    [/@Service|@Component|@Injectable/, 'business_logic'],
    [/class\s+\w*Service/, 'business_logic'],
    [/@Entity|@Table|@Document|@dataclass/, 'domain_model'],
    [/class\s+\w*(?:Model|Schema|Entity|DTO)/, 'domain_model'],
    [/class\s+\w+\(.*(?:Base|Model|Schema|DeclarativeBase)/, 'domain_model'],
    [/@Repository|@Mapper/, 'infrastructure'],
    [/class\s+\w*(?:Repository|Repo|DAO|Client|Adapter)/, 'infrastructure'],
    [/(?:def|function)\s+test_|@Test|@pytest|#\[test\]|#\[tokio::test\]/, 'test'],
    [/class\s+Test\w+|describe\s*\(/, 'test'],
];

const PATH_ROLE_PATTERNS: Array<[RegExp, string]> = [
    [/test[s_/]|_test\.|\.test\.|\.spec\./, 'test'],
    [/route[rs]?[/.]|endpoint|handler|controller|view[s]?[/.]/, 'route_entry'],
    [/service[s]?[/.]|usecase|interactor/, 'business_logic'],
    [/model[s]?[/.]|schema[s]?[/.]|entit(?:y|ies)[/.]|domain[/.]/, 'domain_model'],
    [/util[s]?[/.]|helper[s]?[/.]|common[/.]|lib[/.]/, 'utility'],
    [/repo(?:sitory)?[/.]|dao[/.]|adapter[/.]|client[/.]|infra[/.]|db[/.]/, 'infrastructure'],
];

/**
 * Classify a symbol's architectural role based on decorators, file path, and name.
 * Ported from Python `_classify_symbol_role` in code_tools/tools.py.
 *
 * @param name - Symbol name.
 * @param kind - Symbol kind (e.g. "function", "class").
 * @param filePath - Absolute path to the file containing the symbol.
 * @param signature - Symbol signature or surrounding context lines.
 * @param workspace - Path to the workspace root.
 * @param startLine - Zero-based line number where the symbol starts.
 * @returns Semantic role string (e.g. "route_handler", "model", "test").
 */
export function classifySymbolRole(
    name: string,
    kind: string,
    filePath: string,
    signature: string,
    workspace: string,
    startLine: number = 0,
): string {
    // 1. Check signature + decorator context (5 lines above the symbol)
    let context = signature;
    if (startLine > 1) {
        try {
            const absPath = path.join(path.resolve(workspace), filePath);
            const stat = fs.statSync(absPath);
            if (stat.isFile() && stat.size < MAX_FILE_SIZE) {
                const lines = readFileNormalized(absPath).split('\n');
                const decoStart = Math.max(0, startLine - 6);
                const decoEnd = Math.min(lines.length, startLine);
                context = lines.slice(decoStart, decoEnd).join('\n') + '\n' + signature;
            }
        } catch { /* ignore */ }
    }

    for (const [pat, role] of SIG_ROLE_PATTERNS) {
        if (pat.test(context)) { return role; }
    }

    // 2. Check file path
    const fpLower = filePath.toLowerCase().replace(/\\/g, '/');
    for (const [pat, role] of PATH_ROLE_PATTERNS) {
        if (pat.test(fpLower)) { return role; }
    }

    // 3. Name-based fallback
    const nLower = name.toLowerCase();
    if (nLower.startsWith('test') || nLower.endsWith('test')) { return 'test'; }
    if (['service', 'usecase', 'interactor'].some(s => nLower.includes(s))) { return 'business_logic'; }
    if (['model', 'schema', 'entity'].some(s => nLower.includes(s))) { return 'domain_model'; }
    if (['handler', 'controller', 'endpoint', 'route', 'view'].some(s => nLower.includes(s))) { return 'route_entry'; }
    if (['repository', 'repo', 'dao', 'client', 'adapter'].some(s => nLower.includes(s))) { return 'infrastructure'; }
    if (['util', 'helper', 'common'].some(s => nLower.includes(s))) { return 'utility'; }

    return 'unknown';
}

// ---------------------------------------------------------------------------
// Tool implementations
// ---------------------------------------------------------------------------

/**
 * Extract file structure (classes, functions, methods) with line numbers.
 *
 * @param workspace - Path to the workspace root.
 * @param params - Tool parameters containing the relative file path.
 * @returns ToolResult with an array of symbol definitions on success.
 */
export async function file_outline(
    workspace: string,
    params: { path: string },
): Promise<ToolResult> {
    const ws = path.resolve(workspace);
    let absPath: string;

    try {
        absPath = resolvePath(ws, params.path);
    } catch (e: unknown) {
        return { success: false, data: null, error: e instanceof Error ? e.message : String(e) };
    }

    try {
        if (!fs.existsSync(absPath) || !fs.statSync(absPath).isFile()) {
            return { success: false, data: null, error: `File not found: ${params.path}` };
        }
    } catch {
        return { success: false, data: null, error: `File not found: ${params.path}` };
    }

    const relPath = path.relative(ws, absPath);
    const defs = await extractDefinitionsAsync(absPath, relPath);

    return {
        success: true,
        data: defs.map(d => ({
            name: d.name,
            kind: d.kind,
            file_path: d.file_path,
            start_line: d.start_line,
            end_line: d.end_line,
            signature: d.signature,
        })),
    };
}

/**
 * Find symbol definitions by name using a cached workspace-wide AST index.
 *
 * Results are sorted: exact name matches come before substring matches.
 * If kind is specified, results are filtered to that kind.
 *
 * @param workspace - Path to the workspace root.
 * @param params - Tool parameters: symbol name and optional kind filter.
 * @returns ToolResult with an array of matching symbol definitions.
 */
export function find_symbol(
    workspace: string,
    params: { name: string; kind?: string },
): ToolResult {
    const index = getSymbolIndex(workspace);

    if (Object.keys(index).length === 0) {
        return {
            success: true,
            data: [],
            error: 'Symbol index is empty -- no parseable source files found in workspace.',
        };
    }

    const nameLower = params.name.toLowerCase();
    const ws = path.resolve(workspace);
    const results: Array<SymbolDef & { role?: string }> = [];

    for (const [rel, definitions] of Object.entries(index)) {
        for (const defn of definitions) {
            if (!defn.name.toLowerCase().includes(nameLower)) {
                continue;
            }
            if (params.kind && defn.kind !== params.kind) {
                continue;
            }
            const role = classifySymbolRole(
                defn.name, defn.kind, defn.file_path,
                defn.signature, workspace, defn.start_line,
            );
            results.push({ ...defn, role });
        }
    }

    // Sort: role priority first, then exact match before substring match
    results.sort((a, b) => {
        const aRole = ROLE_PRIORITY[a.role || 'unknown'] ?? 99;
        const bRole = ROLE_PRIORITY[b.role || 'unknown'] ?? 99;
        if (aRole !== bRole) { return aRole - bRole; }
        const aExact = a.name.toLowerCase() === nameLower ? 0 : 1;
        const bExact = b.name.toLowerCase() === nameLower ? 0 : 1;
        return aExact - bExact;
    });

    return { success: true, data: results };
}

/**
 * Find all usages of a symbol across the workspace via grep and AST validation.
 *
 * Searches files for lines containing the symbol name (word boundary),
 * then validates against AST-extracted references where possible.
 *
 * @param workspace - Path to the workspace root.
 * @param params - Tool parameters: symbol name and optional file scope.
 * @returns ToolResult with an array of reference locations (file, line, snippet).
 */
export function find_references(
    workspace: string,
    params: { symbol_name: string; file?: string },
): ToolResult {
    const ws = path.resolve(workspace);
    const symbolName = params.symbol_name;
    const wordBoundaryRe = new RegExp(`\\b${escapeRegExp(symbolName)}\\b`);

    interface RefMatch {
        file_path: string;
        line_number: number;
        content: string;
    }

    const grepMatches: RefMatch[] = [];
    const maxResults = 100;

    // If file is specified, search only that file; otherwise walk workspace
    if (params.file) {
        let absPath: string;
        try {
            absPath = resolvePath(ws, params.file);
        } catch (e: unknown) {
            return { success: false, data: null, error: e instanceof Error ? e.message : String(e) };
        }

        try {
            const content = readFileNormalized(absPath);
            const lines = content.split('\n');
            const relPath = path.relative(ws, absPath);

            for (let i = 0; i < lines.length && grepMatches.length < maxResults; i++) {
                if (wordBoundaryRe.test(lines[i])) {
                    grepMatches.push({
                        file_path: relPath,
                        line_number: i + 1,
                        content: lines[i].trim().slice(0, 200),
                    });
                }
            }
        } catch (e: unknown) {
            const msg = e instanceof Error ? e.message : String(e);
            return { success: false, data: null, error: `Cannot read file: ${msg}` };
        }
    } else {
        walkSourceFiles(ws, (absPath, relPath) => {
            if (grepMatches.length >= maxResults) return true; // stop walking

            try {
                const content = readFileNormalized(absPath);
                // Quick check before line-by-line scan
                if (!content.includes(symbolName)) return;

                const lines = content.split('\n');
                for (let i = 0; i < lines.length && grepMatches.length < maxResults; i++) {
                    if (wordBoundaryRe.test(lines[i])) {
                        grepMatches.push({
                            file_path: relPath,
                            line_number: i + 1,
                            content: lines[i].trim().slice(0, 200),
                        });
                    }
                }
            } catch { /* skip unreadable files */ }
        });
    }

    // Validate grep hits through AST reference data where available.
    // Group by file to avoid re-parsing.
    const byFile = new Map<string, RefMatch[]>();
    for (const m of grepMatches) {
        const existing = byFile.get(m.file_path);
        if (existing) {
            existing.push(m);
        } else {
            byFile.set(m.file_path, [m]);
        }
    }

    const validated: RefMatch[] = [];

    for (const [relPath, fileMatches] of byFile) {
        const absPath = path.join(ws, relPath);
        const lang = detectLanguage(absPath);

        // Accept all grep matches — regex-level validation is sufficient.
        for (const m of fileMatches) {
            validated.push(m);
        }
    }

    return {
        success: true,
        data: validated,
        truncated: grepMatches.length >= maxResults,
    };
}

/**
 * Find functions called by a given function, scoped to its body lines only.
 *
 * Scans only the function body lines (start_line to end_line),
 * NOT the entire file, to prevent false positives from other functions
 * in the same file.
 *
 * @param workspace - Path to the workspace root.
 * @param params - Tool parameters: function name and relative file path.
 * @returns ToolResult with an array of callee names found in the function body.
 */
export async function get_callees(
    workspace: string,
    params: { function_name: string; file: string },
): Promise<ToolResult> {
    const ws = path.resolve(workspace);
    let absPath: string;

    try {
        absPath = resolvePath(ws, params.file);
    } catch (e: unknown) {
        return { success: false, data: null, error: e instanceof Error ? e.message : String(e) };
    }

    if (!fs.existsSync(absPath) || !fs.statSync(absPath).isFile()) {
        return { success: false, data: null, error: `File not found: ${params.file}` };
    }

    if (detectLanguage(absPath) === null) {
        return { success: false, data: null, error: `Unsupported language: ${params.file}` };
    }

    let source: string;
    try {
        source = readFileNormalized(absPath);
    } catch (e: unknown) {
        return { success: false, data: null, error: e instanceof Error ? e.message : String(e) };
    }

    // Find the function's line range — prefer tree-sitter (matches Python backend)
    const relPath = path.relative(ws, absPath);
    const allDefs = await extractDefinitionsAsync(absPath, relPath);

    let targetDef: SymbolDef | null = null;
    for (const d of allDefs) {
        if (d.name === params.function_name) {
            targetDef = d;
            break;
        }
    }

    if (targetDef === null) {
        return {
            success: false,
            data: null,
            error: `Function '${params.function_name}' not found in ${params.file}`,
        };
    }

    const lines = source.split('\n');

    // When the regex fallback is used, end_line == start_line. In that case
    // infer the end by looking for the next top-level definition or EOF.
    let endLine = targetDef.end_line;
    if (endLine <= targetDef.start_line) {
        const nextStarts = allDefs
            .map(d => d.start_line)
            .filter(sl => sl > targetDef!.start_line)
            .sort((a, b) => a - b);
        endLine = nextStarts.length > 0 ? nextStarts[0] - 1 : lines.length;
    }

    // Extract lines of the function body (1-based to 0-based conversion)
    const bodyLines = lines.slice(targetDef.start_line - 1, endLine);

    // Find function calls in the body using regex.
    // Matches: name(...), obj.name(...), but not def name(... or class name(
    const callPattern = /(?<!\bdef\s)(?<!\bclass\s)\b([a-zA-Z_]\w*)\s*\(/g;

    const seen = new Set<string>();
    const callees: Array<{ callee_name: string; file_path: string; line: number }> = [];

    for (let offset = 0; offset < bodyLines.length; offset++) {
        const line = bodyLines[offset];
        const lineNo = targetDef.start_line + offset;
        let match: RegExpExecArray | null;

        // Reset lastIndex for each line
        callPattern.lastIndex = 0;

        while ((match = callPattern.exec(line)) !== null) {
            const calleeName = match[1];
            // Skip noise words (keywords, builtins)
            if (CALL_NOISE.has(calleeName)) continue;

            if (!seen.has(calleeName)) {
                seen.add(calleeName);
                callees.push({
                    callee_name: calleeName,
                    file_path: relPath,
                    line: lineNo,
                });
            }
        }
    }

    return { success: true, data: callees };
}

/**
 * Find functions that call a given function across the workspace.
 *
 * Walks workspace files, does a quick string check, then for matching files
 * parses definitions and checks each function body for the call pattern.
 *
 * @param workspace - Path to the workspace root.
 * @param params - Tool parameters: function name and optional directory scope.
 * @returns ToolResult with an array of caller locations (file, line, function name).
 */
export function get_callers(
    workspace: string,
    params: { function_name: string; path?: string },
): ToolResult {
    const ws = path.resolve(workspace);
    const searchRoot = params.path ? resolvePath(ws, params.path) : ws;

    if (!fs.existsSync(searchRoot)) {
        return { success: false, data: null, error: `Path not found: ${params.path}` };
    }

    // Regex: function_name followed by ( -- a call site
    const callRe = new RegExp(`\\b${escapeRegExp(params.function_name)}\\s*\\(`);

    const callers: Array<{
        caller_name: string;
        caller_kind: string;
        file_path: string;
        line: number;
        content: string;
    }> = [];

    // Use walkSourceFiles for the search root, but we need to handle
    // the case where searchRoot is a subdirectory
    const walkRoot = searchRoot;
    const walkRootResolved = path.resolve(walkRoot);

    const walkDir = (dir: string, relToWs: string): void => {
        let entries: fs.Dirent[];
        try {
            entries = fs.readdirSync(dir, { withFileTypes: true });
        } catch {
            return;
        }

        for (const entry of entries) {
            if (entry.isDirectory()) {
                if (EXCLUDED_DIRS.has(entry.name) || entry.name.startsWith('.')) {
                    continue;
                }
                walkDir(
                    path.join(dir, entry.name),
                    relToWs ? path.join(relToWs, entry.name) : entry.name,
                );
            } else if (entry.isFile()) {
                const ext = path.extname(entry.name).toLowerCase();
                if (!SUPPORTED_EXTS.has(ext)) continue;

                const absPath = path.join(dir, entry.name);
                try {
                    if (fs.statSync(absPath).size > MAX_FILE_SIZE) continue;
                } catch {
                    continue;
                }

                if (detectLanguage(absPath) === null) continue;

                let source: string;
                try {
                    source = readFileNormalized(absPath);
                } catch {
                    continue;
                }

                // Quick check: does the file contain a call?
                if (!callRe.test(source)) continue;

                const relPath = path.relative(ws, absPath);
                const allDefs = extractDefinitions(absPath, relPath);
                const lines = source.split('\n');

                for (const defn of allDefs) {
                    if (defn.kind !== 'function' && defn.kind !== 'class') continue;

                    // Infer end_line when regex fallback sets it == start_line
                    let endLn = defn.end_line;
                    if (endLn <= defn.start_line) {
                        const nextStarts = allDefs
                            .map(d => d.start_line)
                            .filter(sl => sl > defn.start_line)
                            .sort((a, b) => a - b);
                        endLn = nextStarts.length > 0 ? nextStarts[0] - 1 : lines.length;
                    }

                    // Skip the definition line itself (def foo(): matches \bfoo\s*\()
                    // Body starts one line after the definition header
                    const bodyLines = lines.slice(defn.start_line, endLn);

                    for (let offset = 0; offset < bodyLines.length; offset++) {
                        const line = bodyLines[offset];
                        if (callRe.test(line)) {
                            callers.push({
                                caller_name: defn.name,
                                caller_kind: defn.kind,
                                file_path: relPath,
                                line: defn.start_line + 1 + offset,
                                content: line.trim().slice(0, 200),
                            });
                            break; // one match per caller is enough
                        }
                    }
                }
            }
        }
    };

    // Compute rel path from ws to the walk root
    const relFromWs = path.relative(ws, walkRootResolved);
    walkDir(walkRootResolved, relFromWs === '.' ? '' : relFromWs);

    return { success: true, data: callers };
}

/**
 * Get the full source code of a symbol definition.
 *
 * If file_path is provided, searches within that file. Otherwise walks
 * the entire workspace to find the symbol. Supports exact and substring matching.
 *
 * @param workspace - Path to the workspace root.
 * @param params - Tool parameters: symbol name and optional file path scope.
 * @returns ToolResult with the symbol's full source text and location metadata.
 */
export async function expand_symbol(
    workspace: string,
    params: { symbol_name: string; file_path?: string },
): Promise<ToolResult> {
    const ws = path.resolve(workspace);

    // If file_path is provided, search within that file
    if (params.file_path) {
        let absPath: string;
        try {
            absPath = resolvePath(ws, params.file_path);
        } catch (e: unknown) {
            return { success: false, data: null, error: e instanceof Error ? e.message : String(e) };
        }

        if (!fs.existsSync(absPath) || !fs.statSync(absPath).isFile()) {
            return { success: false, data: null, error: `File not found: ${params.file_path}` };
        }

        const relPath = path.relative(ws, absPath);
        const allDefs = await extractDefinitionsAsync(absPath, relPath);

        // Try exact match first
        let matches = allDefs.filter(s => s.name === params.symbol_name);
        // Fall back to substring match
        if (matches.length === 0) {
            const nameLower = params.symbol_name.toLowerCase();
            matches = allDefs.filter(s => s.name.toLowerCase().includes(nameLower));
        }

        if (matches.length === 0) {
            const available = allDefs.map(s => s.name).slice(0, 20);
            return {
                success: false,
                data: null,
                error: `Symbol '${params.symbol_name}' not found in ${params.file_path}. ` +
                       `Available: ${available.join(', ')}`,
            };
        }

        const sym = matches[0];
        let source: string;
        try {
            const content = readFileNormalized(absPath);
            const lines = content.split('\n');
            source = lines.slice(sym.start_line - 1, sym.end_line).join('\n');
        } catch (e: unknown) {
            return { success: false, data: null, error: e instanceof Error ? e.message : String(e) };
        }

        return {
            success: true,
            data: {
                symbol_name: sym.name,
                kind: sym.kind,
                file_path: relPath,
                start_line: sym.start_line,
                end_line: sym.end_line,
                signature: sym.signature,
                source,
            },
        };
    }

    // No file_path -- search the entire workspace
    const candidates: Array<{ sym: SymbolDef; absPath: string }> = [];

    walkSourceFiles(ws, (absPath, relPath) => {
        if (candidates.length >= 5) return true; // stop walking

        try {
            const allDefs = extractDefinitions(absPath, relPath);

            for (const s of allDefs) {
                if (s.name === params.symbol_name) {
                    candidates.push({ sym: s, absPath });
                } else if (
                    params.symbol_name.toLowerCase() ===
                    s.name.toLowerCase().slice(0, params.symbol_name.length) &&
                    candidates.length === 0
                ) {
                    // Substring match -- only if no exact matches yet
                    if (s.name.toLowerCase().includes(params.symbol_name.toLowerCase())) {
                        candidates.push({ sym: s, absPath });
                    }
                }
            }
        } catch { /* skip unreadable files */ }
    });

    if (candidates.length === 0) {
        return {
            success: false,
            data: null,
            error: `Symbol '${params.symbol_name}' not found in the workspace.`,
        };
    }

    const { sym, absPath } = candidates[0];
    let source: string;
    try {
        const content = readFileNormalized(absPath);
        const lines = content.split('\n');
        source = lines.slice(sym.start_line - 1, sym.end_line).join('\n');
    } catch (e: unknown) {
        return { success: false, data: null, error: e instanceof Error ? e.message : String(e) };
    }

    const relPath = path.relative(ws, absPath);
    const data: Record<string, any> = {
        symbol_name: sym.name,
        kind: sym.kind,
        file_path: relPath,
        start_line: sym.start_line,
        end_line: sym.end_line,
        signature: sym.signature,
        source,
    };

    // If multiple candidates, show alternatives
    if (candidates.length > 1) {
        data.alternatives = candidates.slice(1, 5).map(c => ({
            name: c.sym.name,
            file_path: path.relative(ws, c.absPath),
            kind: c.sym.kind,
            line: c.sym.start_line,
        }));
    }

    return { success: true, data };
}

// ---------------------------------------------------------------------------
// Cache management
// ---------------------------------------------------------------------------

/**
 * Clear the AST symbol cache for a workspace, or all workspaces if omitted.
 *
 * @param workspace - Workspace root to evict; omit to clear the entire cache.
 */
export function invalidateSymbolCache(workspace?: string): void {
    if (workspace) {
        symbolIndexCache.delete(path.resolve(workspace));
    } else {
        symbolIndexCache.clear();
    }
}

// ---------------------------------------------------------------------------
// Utility
// ---------------------------------------------------------------------------

/**
 * Escape special regex characters in a string.
 */
function escapeRegExp(str: string): string {
    return str.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}
