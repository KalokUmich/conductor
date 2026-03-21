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
 * Uses symbolExtractor.ts (no tree-sitter dependency) for AST extraction,
 * and synchronous file I/O since tool execution runs in a worker context.
 *
 * @module services/astToolRunner
 */

import * as fs from 'fs';
import * as path from 'path';
import { execSync } from 'child_process';
import { extractSymbols, type FileSymbol } from './symbolExtractor';
import * as treeSitter from './treeSitterService';

// Re-export types from repoGraphBuilder for consumers that need them.
import type { SymbolDef, FileSymbolsData } from './repoGraphBuilder';

/**
 * Read a file as UTF-8 text, normalizing \r\n → \n to match Python behavior.
 */
function readFileNormalized(absPath: string): string {
    return fs.readFileSync(absPath, 'utf-8').replace(/\r\n/g, '\n');
}

// ---------------------------------------------------------------------------
// Public result type
// ---------------------------------------------------------------------------

export interface ToolResult {
    success: boolean;
    data: any;
    error?: string;
    truncated?: boolean;
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

/**
 * Convert FileSymbol (0-based lines from symbolExtractor) to SymbolDef (1-based lines).
 */
function toSymbolDef(sym: FileSymbol, relPath: string): SymbolDef {
    return {
        name: sym.name,
        kind: sym.kind,
        file_path: relPath,
        start_line: sym.range.start.line + 1,
        end_line: sym.range.end.line + 1,
        signature: sym.signature,
    };
}

/**
 * Extract definitions from a file, returning 1-based SymbolDefs.
 *
 * Prefers web-tree-sitter (same quality as Python backend) when initialized.
 * Falls back to regex-based symbolExtractor when tree-sitter is unavailable.
 */
async function extractDefinitionsAsync(absPath: string, relPath: string): Promise<SymbolDef[]> {
    // Try tree-sitter first (matches Python backend quality)
    if (treeSitter.isInitialized()) {
        try {
            const source = fs.readFileSync(absPath);
            // Pass relPath so file_path in SymbolDef is relative (matching Python)
            const result = await treeSitter.extractDefinitions(relPath, source);
            return result.definitions;
        } catch {
            // Fall through to regex
        }
    }
    // Regex fallback
    return extractDefinitionsSync(absPath, relPath);
}

/**
 * Synchronous regex-based extraction (used as fallback and for workspace-wide scans).
 */
function extractDefinitionsSync(absPath: string, relPath: string): SymbolDef[] {
    const extracted = extractSymbols(absPath);
    return extracted.symbols.map(s => toSymbolDef(s, relPath));
}

/** Backwards-compatible sync alias used by find_symbol index building. */
function extractDefinitions(absPath: string, relPath: string): SymbolDef[] {
    return extractDefinitionsSync(absPath, relPath);
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
 * Get all definitions in a file.
 */
export async function file_outline(
    workspace: string,
    params: { path: string },
): Promise<ToolResult> {
    const ws = path.resolve(workspace);
    let absPath: string;

    try {
        absPath = resolvePath(ws, params.path);
    } catch (e: any) {
        return { success: false, data: null, error: e.message };
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
 * Find symbol definitions using a cached workspace-wide symbol index.
 *
 * Results are sorted: exact name matches come before substring matches.
 * If kind is specified, results are filtered to that kind.
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
 * Find references to a symbol via grep + AST validation.
 *
 * Searches files for lines containing the symbol name (word boundary),
 * then validates against AST-extracted references where possible.
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
        } catch (e: any) {
            return { success: false, data: null, error: e.message };
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
        } catch (e: any) {
            return { success: false, data: null, error: `Cannot read file: ${e.message}` };
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

        if (lang !== null) {
            try {
                const extracted = extractSymbols(absPath);
                // Build a set of lines where imports reference this symbol
                const refLines = new Set<number>();

                // Check import references
                for (const imp of extracted.imports) {
                    if (imp.includes(symbolName)) {
                        // We don't have exact line info for imports from symbolExtractor,
                        // so we accept all grep matches from this file that hit imports
                    }
                }

                // The symbolExtractor doesn't provide per-reference line data like
                // the Python parser does, so we accept all grep matches as valid.
                // This matches the Python fallback behavior.
                for (const m of fileMatches) {
                    validated.push(m);
                }
            } catch {
                // Fallback: keep grep matches as-is
                for (const m of fileMatches) {
                    validated.push(m);
                }
            }
        } else {
            // Non-parseable files: keep grep matches as-is
            for (const m of fileMatches) {
                validated.push(m);
            }
        }
    }

    return {
        success: true,
        data: validated,
        truncated: grepMatches.length >= maxResults,
    };
}

/**
 * Find all functions/methods called within a specific function body.
 *
 * Critical: scans only the function body lines (start_line to end_line),
 * NOT the entire file. This prevents false positives from other functions
 * in the same file.
 */
export async function get_callees(
    workspace: string,
    params: { function_name: string; file: string },
): Promise<ToolResult> {
    const ws = path.resolve(workspace);
    let absPath: string;

    try {
        absPath = resolvePath(ws, params.file);
    } catch (e: any) {
        return { success: false, data: null, error: e.message };
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
    } catch (e: any) {
        return { success: false, data: null, error: e.message };
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
 * Find all functions/methods that call a given function.
 *
 * Walks workspace files, does a quick string check, then for matching files
 * parses definitions and checks each function body for the call pattern.
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
                const extracted = extractSymbols(absPath);
                const allDefs = extracted.symbols.map(s => toSymbolDef(s, relPath));
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
 * Expand a symbol to its full source code.
 *
 * If file_path is provided, searches within that file. Otherwise walks
 * the entire workspace to find the symbol. Supports exact and substring matching.
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
        } catch (e: any) {
            return { success: false, data: null, error: e.message };
        }

        if (!fs.existsSync(absPath) || !fs.statSync(absPath).isFile()) {
            return { success: false, data: null, error: `File not found: ${params.file_path}` };
        }

        const relPath = path.relative(ws, absPath);
        const defs = await extractDefinitionsAsync(absPath, relPath);
        const extracted = { symbols: defs.map(d => ({ name: d.name, kind: d.kind as any, signature: d.signature, range: { start: { line: d.start_line - 1, character: 0 }, end: { line: d.end_line - 1, character: 0 } } })) };
        const allDefs = extracted.symbols.map(s => toSymbolDef(s, relPath));

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
        } catch (e: any) {
            return { success: false, data: null, error: e.message };
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
            const extracted = extractSymbols(absPath);
            const allDefs = extracted.symbols.map(s => toSymbolDef(s, relPath));

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
    } catch (e: any) {
        return { success: false, data: null, error: e.message };
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
 * Clear the symbol index cache for a workspace (or all workspaces).
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
