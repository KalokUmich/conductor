/**
 * treeSitterService.ts
 *
 * AST-based symbol extraction using web-tree-sitter (WASM).
 *
 * Ported from backend/app/repo_graph/parser.py -- extracts definitions
 * (functions, classes, methods, interfaces) and references (identifiers)
 * from source files.  Used by the extension-side repo graph builder.
 *
 * Supported languages: Python, JavaScript, TypeScript, Java, Go, Rust, C, C++.
 * Falls back to regex extraction when a grammar .wasm is not available.
 */

import * as path from "path";
import { Parser, Language, Node as SyntaxNode } from "web-tree-sitter";

// ---------------------------------------------------------------------------
// Data types (mirrors Python SymbolDef / SymbolRef / FileSymbols)
// ---------------------------------------------------------------------------

export interface SymbolDef {
    name: string;
    kind: string; // "function" | "class" | "method" | "interface" | "type" | "symbol"
    file_path: string;
    start_line: number;
    end_line: number;
    signature: string;
}

export interface SymbolRef {
    name: string;
    file_path: string;
    line: number;
}

export interface FileSymbols {
    file_path: string;
    definitions: SymbolDef[];
    references: SymbolRef[];
    language: string | null;
}

// ---------------------------------------------------------------------------
// Language detection (ported from _EXT_TO_LANG)
// ---------------------------------------------------------------------------

const EXT_TO_LANG: Record<string, string> = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
};

/**
 * Detect programming language from a file extension.
 * Returns null if the extension is not recognized.
 */
export function detectLanguage(filePath: string): string | null {
    const ext = path.extname(filePath).toLowerCase();
    return EXT_TO_LANG[ext] ?? null;
}

// ---------------------------------------------------------------------------
// Tree-sitter AST node types that represent definitions
// ---------------------------------------------------------------------------

const DEF_NODE_TYPES = new Set<string>([
    // Python
    "function_definition",
    "class_definition",
    // JS / TS / Go
    "function_declaration",
    "class_declaration",
    // JS / TS
    "method_definition",
    // Java / Go
    "method_declaration",
    // Rust
    "function_item",
    "struct_item",
    "impl_item",
    // TS / Java
    "interface_declaration",
    // TS
    "type_alias_declaration",
]);

const KIND_MAP: Record<string, string> = {
    function_definition: "function",
    function_declaration: "function",
    function_item: "function",
    class_definition: "class",
    class_declaration: "class",
    struct_item: "class",
    impl_item: "class",
    interface_declaration: "interface",
    method_definition: "method",
    method_declaration: "method",
    type_alias_declaration: "type",
};

/** AST child node types that carry the name of a definition. */
const NAME_NODE_TYPES = new Set<string>([
    "identifier",
    "name",
    "property_identifier",
    "type_identifier",
]);

// ---------------------------------------------------------------------------
// Python keywords to filter out of references
// ---------------------------------------------------------------------------

const PYTHON_KEYWORDS = new Set<string>([
    "False", "None", "True", "and", "as", "assert", "async", "await",
    "break", "class", "continue", "def", "del", "elif", "else", "except",
    "finally", "for", "from", "global", "if", "import", "in", "is",
    "lambda", "nonlocal", "not", "or", "pass", "raise", "return",
    "try", "while", "with", "yield", "self", "cls",
]);

// ---------------------------------------------------------------------------
// Regex fallback patterns (ported from _PATTERNS)
// ---------------------------------------------------------------------------

interface RegexPatternEntry {
    kind: string;
    pattern: RegExp;
}

const REGEX_PATTERNS: Record<string, RegexPatternEntry[]> = {
    python: [
        { kind: "function", pattern: /^(?:async\s+)?def\s+(\w+)\s*\(/gm },
        { kind: "class", pattern: /^class\s+(\w+)\s*[:(]/gm },
    ],
    javascript: [
        { kind: "function", pattern: /(?:async\s+)?function\s+(\w+)\s*\(/gm },
        { kind: "class", pattern: /class\s+(\w+)\s*[{]/gm },
        { kind: "function", pattern: /(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\(/gm },
    ],
    typescript: [
        { kind: "function", pattern: /(?:async\s+)?function\s+(\w+)\s*[(<]/gm },
        { kind: "class", pattern: /class\s+(\w+)\s*[{<]/gm },
        { kind: "interface", pattern: /interface\s+(\w+)\s*[{<]/gm },
    ],
    java: [
        { kind: "class", pattern: /(?:public|private|protected|abstract|final|static)?\s*class\s+(\w+)\s*[{<(]/gm },
        { kind: "interface", pattern: /(?:public|private|protected)?\s*interface\s+(\w+)\s*[{<]/gm },
        { kind: "class", pattern: /(?:public|private|protected)?\s*enum\s+(\w+)\s*[{]/gm },
        { kind: "class", pattern: /(?:public|private|protected)?\s*record\s+(\w+)\s*[(<]/gm },
        { kind: "method", pattern: /^\s+(?:public|private|protected)\s+(?:static\s+)?(?:synchronized\s+)?(?:final\s+)?(?:[\w<>\[\],\s]+?)\s+(\w+)\s*\(/gm },
    ],
    go: [
        { kind: "function", pattern: /^func\s+(\w+)\s*\(/gm },
        { kind: "method", pattern: /^func\s+\([^)]+\)\s+(\w+)\s*\(/gm },
        { kind: "class", pattern: /^type\s+(\w+)\s+struct\s*\{/gm },
        { kind: "interface", pattern: /^type\s+(\w+)\s+interface\s*\{/gm },
    ],
    rust: [
        { kind: "function", pattern: /(?:pub\s+)?(?:async\s+)?fn\s+(\w+)/gm },
        { kind: "class", pattern: /(?:pub\s+)?struct\s+(\w+)/gm },
        { kind: "class", pattern: /(?:pub\s+)?enum\s+(\w+)/gm },
        { kind: "interface", pattern: /(?:pub\s+)?trait\s+(\w+)/gm },
        { kind: "class", pattern: /impl(?:<[^>]+>)?\s+(\w+)/gm },
    ],
    c: [
        { kind: "function", pattern: /^(?:static\s+)?(?:inline\s+)?(?:const\s+)?(?:unsigned\s+)?(?:struct\s+)?\w[\w*\s]+?\s+(\w+)\s*\([^;]*$/gm },
        { kind: "class", pattern: /(?:typedef\s+)?struct\s+(\w+)\s*\{/gm },
        { kind: "class", pattern: /(?:typedef\s+)?enum\s+(\w+)\s*\{/gm },
    ],
    cpp: [
        { kind: "function", pattern: /^(?:static\s+)?(?:virtual\s+)?(?:inline\s+)?(?:const\s+)?[\w:*&<>\s]+?\s+(\w+)\s*\([^;]*$/gm },
        { kind: "class", pattern: /(?:class|struct)\s+(\w+)\s*[{:]/gm },
        { kind: "interface", pattern: /namespace\s+(\w+)\s*\{/gm },
    ],
};

/** Reference pattern: identifiers that look like symbol references. */
const REF_PATTERN = /\b([A-Z][a-zA-Z0-9_]*|[a-z_][a-zA-Z0-9_]{2,})\b/g;

// ---------------------------------------------------------------------------
// Module state: parser cache and initialization flag
// ---------------------------------------------------------------------------

let _initialized = false;
let _grammarsPath = "";

/** Cache of loaded Language objects, keyed by language name. */
const _languageCache: Map<string, Language> = new Map();

/** Cache of Parser instances (one per language). */
const _parserCache: Map<string, Parser> = new Map();

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Initialize the web-tree-sitter runtime.
 *
 * Must be called once before any parsing. Typically called during extension
 * activation with `context.extensionPath`.
 *
 * @param extensionPath  Absolute path to the extension root directory.
 *                       The tree-sitter.wasm runtime and grammar .wasm files
 *                       are expected under `<extensionPath>/grammars/`.
 */
export async function initTreeSitter(extensionPath: string): Promise<void> {
    if (_initialized) {
        return;
    }

    _grammarsPath = path.join(extensionPath, "grammars");

    await Parser.init({
        locateFile(file: string) {
            return path.join(_grammarsPath, file);
        },
    });

    _initialized = true;
}

/**
 * Get (or create and cache) a tree-sitter Parser for the given language.
 *
 * Returns null if the grammar .wasm file is not available.
 */
async function getParser(language: string): Promise<Parser | null> {
    if (_parserCache.has(language)) {
        return _parserCache.get(language)!;
    }

    const wasmPath = path.join(_grammarsPath, `tree-sitter-${language}.wasm`);

    try {
        let lang = _languageCache.get(language);
        if (!lang) {
            lang = await Language.load(wasmPath);
            _languageCache.set(language, lang);
        }

        const parser = new Parser();
        parser.setLanguage(lang);
        _parserCache.set(language, parser);
        return parser;
    } catch {
        // Grammar file not found or load failure -- fall back to regex
        return null;
    }
}

// ---------------------------------------------------------------------------
// Tree-sitter extraction (ported from _extract_with_tree_sitter)
// ---------------------------------------------------------------------------

/**
 * Walk a tree-sitter AST node tree and collect definitions.
 *
 * Ported from Python `_walk_for_definitions`.
 */
function walkForDefinitions(
    node: SyntaxNode,
    sourceText: string,
    filePath: string,
    definitions: SymbolDef[],
): void {
    if (DEF_NODE_TYPES.has(node.type)) {
        // Find the name child node
        let nameNode: SyntaxNode | null = null;
        for (let i = 0; i < node.childCount; i++) {
            const child = node.child(i);
            if (child && NAME_NODE_TYPES.has(child.type)) {
                nameNode = child;
                break;
            }
        }

        if (nameNode) {
            const name = nameNode.text;
            const kind = KIND_MAP[node.type] ?? "symbol";

            // Build a one-line signature from the first line of the node
            const nodeStart = node.startIndex;
            const nextNewline = sourceText.indexOf("\n", nodeStart);
            const firstLineEnd = nextNewline === -1 ? sourceText.length : nextNewline;
            let signature = sourceText.slice(nodeStart, firstLineEnd).trim();
            if (signature.length > 120) {
                signature = signature.slice(0, 117) + "...";
            }

            definitions.push({
                name,
                kind,
                file_path: filePath,
                start_line: node.startPosition.row + 1,
                end_line: node.endPosition.row + 1,
                signature,
            });
        }
    }

    // Recurse into children
    for (let i = 0; i < node.childCount; i++) {
        const child = node.child(i);
        if (child) {
            walkForDefinitions(child, sourceText, filePath, definitions);
        }
    }
}

/**
 * Walk a tree-sitter AST node tree and collect identifier references.
 *
 * Ported from Python `_walk_for_references`.
 */
function walkForReferences(
    node: SyntaxNode,
    sourceText: string,
    filePath: string,
    references: SymbolRef[],
): void {
    if (node.type === "identifier") {
        const name = node.text;
        if (name.length > 1 && !PYTHON_KEYWORDS.has(name)) {
            references.push({
                name,
                file_path: filePath,
                line: node.startPosition.row + 1,
            });
        }
    }

    for (let i = 0; i < node.childCount; i++) {
        const child = node.child(i);
        if (child) {
            walkForReferences(child, sourceText, filePath, references);
        }
    }
}

/**
 * Extract symbols from source using tree-sitter AST parsing.
 *
 * Ported from Python `_extract_with_tree_sitter`.
 */
async function extractWithTreeSitter(
    sourceText: string,
    language: string,
    filePath: string,
): Promise<FileSymbols> {
    const parser = await getParser(language);
    if (!parser) {
        // No grammar available -- fall back to regex
        return extractWithRegex(sourceText, language, filePath);
    }

    const tree = parser.parse(sourceText);
    if (!tree) {
        return extractWithRegex(sourceText, language, filePath);
    }
    const root = tree.rootNode;

    const definitions: SymbolDef[] = [];
    const references: SymbolRef[] = [];

    walkForDefinitions(root, sourceText, filePath, definitions);
    walkForReferences(root, sourceText, filePath, references);

    return {
        file_path: filePath,
        definitions,
        references,
        language,
    };
}

// ---------------------------------------------------------------------------
// Regex fallback (ported from _extract_with_regex)
// ---------------------------------------------------------------------------

/**
 * Extract symbols using regex patterns.
 * Used when tree-sitter grammar is not available.
 */
function extractWithRegex(
    sourceText: string,
    language: string,
    filePath: string,
): FileSymbols {
    const definitions: SymbolDef[] = [];
    const references: SymbolRef[] = [];
    const lines = sourceText.split("\n");

    // Extract definitions using language-specific patterns
    const patterns = REGEX_PATTERNS[language] ?? REGEX_PATTERNS["python"] ?? [];
    for (const { kind, pattern } of patterns) {
        // Reset regex state (global flag means lastIndex is stateful)
        pattern.lastIndex = 0;
        let match: RegExpExecArray | null;
        while ((match = pattern.exec(sourceText)) !== null) {
            const name = match[1];
            const lineNo = sourceText.slice(0, match.index).split("\n").length;
            let sig = lineNo <= lines.length ? lines[lineNo - 1].trim() : "";
            if (sig.length > 120) {
                sig = sig.slice(0, 117) + "...";
            }
            definitions.push({
                name,
                kind,
                file_path: filePath,
                start_line: lineNo,
                end_line: lineNo,
                signature: sig,
            });
        }
    }

    // Extract references
    for (let lineNo = 0; lineNo < lines.length; lineNo++) {
        REF_PATTERN.lastIndex = 0;
        let match: RegExpExecArray | null;
        while ((match = REF_PATTERN.exec(lines[lineNo])) !== null) {
            const name = match[1];
            if (name.length > 1 && !PYTHON_KEYWORDS.has(name)) {
                references.push({
                    name,
                    file_path: filePath,
                    line: lineNo + 1,
                });
            }
        }
    }

    return {
        file_path: filePath,
        definitions,
        references,
        language,
    };
}

// ---------------------------------------------------------------------------
// Main public extraction function
// ---------------------------------------------------------------------------

/**
 * Extract symbol definitions and references from a source file.
 *
 * This is the main entry point, equivalent to Python's `extract_definitions`.
 *
 * @param filePath  Path to the source file (used for language detection and output).
 * @param source    Raw file contents as a Buffer. The caller is responsible for
 *                  reading the file.
 * @returns         Definitions and references found in the file.
 */
export async function extractDefinitions(
    filePath: string,
    source: Buffer,
): Promise<FileSymbols> {
    const language = detectLanguage(filePath);
    if (!language) {
        return {
            file_path: filePath,
            definitions: [],
            references: [],
            language: null,
        };
    }

    const sourceText = source.toString("utf-8");

    if (!_initialized) {
        // tree-sitter not initialized; use regex fallback
        return extractWithRegex(sourceText, language, filePath);
    }

    try {
        return await extractWithTreeSitter(sourceText, language, filePath);
    } catch {
        // If tree-sitter fails for any reason, fall back to regex
        return extractWithRegex(sourceText, language, filePath);
    }
}

/**
 * Check whether tree-sitter has been initialized.
 * Useful for diagnostics and conditional logic.
 */
export function isInitialized(): boolean {
    return _initialized;
}

/**
 * Get the list of languages that have their grammar .wasm loaded.
 * Only returns languages that have already been lazily loaded via parsing.
 */
export function getLoadedLanguages(): string[] {
    return Array.from(_languageCache.keys());
}
