/**
 * On-demand AST symbol extractor for Conductor.
 *
 * Given a file path, reads the file and extracts:
 *   - imports          — every import/require/from statement
 *   - top-level symbols — functions, classes, interfaces, type aliases, enums,
 *                         and exported variables
 *
 * Parsing strategy
 * ----------------
 * .ts / .tsx / .js / .jsx / .mjs / .cjs
 *     TypeScript compiler API (`ts.createSourceFile`).  Full AST walk of the
 *     top-level statement list — no heuristics, no regex.
 *
 * .py
 *     Line-oriented regex: top-level `def` / `async def` / `class` and
 *     `import` / `from … import` statements.
 *
 * .java
 *     Line-oriented regex: `import`, `class`, `interface`, `enum`, and
 *     method declarations that begin with an access modifier.
 *
 * All other extensions return an empty result silently.
 *
 * Large-file safety
 * -----------------
 * Files larger than MAX_FILE_BYTES are read only up to that limit and the
 * content is truncated at the nearest newline boundary before parsing.
 * This caps memory usage regardless of file size.
 *
 * No VS Code dependency — fully testable under the Node.js test runner.
 *
 * @module services/symbolExtractor
 */

import * as fs from 'fs';
import * as path from 'path';
import * as ts from 'typescript';

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

export type SymbolKind =
    | 'function'
    | 'class'
    | 'interface'
    | 'type'
    | 'enum'
    | 'variable';

export interface SymbolRange {
    start: { line: number; character: number };
    end: { line: number; character: number };
}

export interface FileSymbol {
    name: string;
    kind: SymbolKind;
    /** First line / header of the declaration, whitespace-collapsed, ≤ 200 chars. */
    signature: string;
    range: SymbolRange;
}

export interface ExtractedSymbols {
    /** Raw text of every import/require/from statement found. */
    imports: string[];
    symbols: FileSymbol[];
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/**
 * Maximum bytes read from a file before truncating.
 * Exported so tests can verify the cap without creating huge files.
 */
export const MAX_FILE_BYTES = 512 * 1024; // 512 KB

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Extract imports and top-level symbols from a source file.
 *
 * Returns `{ imports: [], symbols: [] }` on any I/O error or unsupported
 * extension — callers never need to handle errors from this function.
 *
 * @param filePath  Absolute or relative path to the source file.
 */
export function extractSymbols(filePath: string): ExtractedSymbols {
    const content = readSafe(filePath);
    if (content === null) {
        return { imports: [], symbols: [] };
    }

    const ext = path.extname(filePath).toLowerCase();
    switch (ext) {
        case '.ts':
        case '.tsx':
        case '.js':
        case '.jsx':
        case '.mjs':
        case '.cjs':
            return extractFromTypeScript(content, filePath);
        case '.py':
            return extractFromPython(content);
        case '.java':
            return extractFromJava(content);
        default:
            return { imports: [], symbols: [] };
    }
}

// ---------------------------------------------------------------------------
// File reading
// ---------------------------------------------------------------------------

/**
 * Read up to MAX_FILE_BYTES of a file.  If the file exceeds the cap, the
 * content is sliced at the last newline before the boundary so the parser
 * always receives syntactically clean line endings.
 *
 * Returns null on any I/O error (file not found, permission denied, etc.).
 */
function readSafe(filePath: string): string | null {
    try {
        let size: number;
        try {
            size = fs.statSync(filePath).size;
        } catch {
            return null;
        }

        if (size <= MAX_FILE_BYTES) {
            return fs.readFileSync(filePath, 'utf-8');
        }

        // Oversized file: read only the first MAX_FILE_BYTES bytes.
        const buf = Buffer.allocUnsafe(MAX_FILE_BYTES);
        const fd = fs.openSync(filePath, 'r');
        try {
            fs.readSync(fd, buf, 0, MAX_FILE_BYTES, 0);
        } finally {
            fs.closeSync(fd);
        }
        const text = buf.toString('utf-8');
        // Trim to the last newline to avoid a broken multi-byte sequence or
        // a half-written token at the cut point.
        const lastNl = text.lastIndexOf('\n');
        return lastNl > 0 ? text.slice(0, lastNl) : text;
    } catch {
        return null;
    }
}

// ---------------------------------------------------------------------------
// TypeScript / JavaScript extractor
// ---------------------------------------------------------------------------

function tsScriptKind(filePath: string): ts.ScriptKind {
    switch (path.extname(filePath).toLowerCase()) {
        case '.tsx': return ts.ScriptKind.TSX;
        case '.jsx': return ts.ScriptKind.JSX;
        case '.js':
        case '.mjs':
        case '.cjs': return ts.ScriptKind.JS;
        default:     return ts.ScriptKind.TS;
    }
}

/** Convert a TypeScript AST node's span to our SymbolRange (0-based lines). */
function toRange(node: ts.Node, src: ts.SourceFile): SymbolRange {
    const s = src.getLineAndCharacterOfPosition(node.getStart(src));
    const e = src.getLineAndCharacterOfPosition(node.getEnd());
    return {
        start: { line: s.line, character: s.character },
        end:   { line: e.line, character: e.character },
    };
}

/**
 * Build a compact, single-line signature for a top-level declaration.
 *
 * For declarations with a body block (functions, classes, …) the signature
 * is the text up to but not including the opening `{`.
 * For variable statements the first source line is used (stripping any
 * trailing `{` so object-literal initialisers don't corrupt the output).
 * In all cases the result is whitespace-collapsed and capped at 200 chars.
 */
function signatureOf(node: ts.Node, src: ts.SourceFile): string {
    const start = node.getStart(src);
    const raw   = src.text.slice(start, node.getEnd());

    let sig: string;

    if (ts.isVariableStatement(node)) {
        // Take the first source line; strip a trailing `{` (object literal).
        const nlIdx = raw.indexOf('\n');
        const line  = nlIdx > 0 ? raw.slice(0, nlIdx) : raw;
        sig = line.replace(/\s*\{[\s\S]*/, '').trim();
        // Fallback: stripping brace removed everything (e.g. `export const x = {`)
        if (!sig) sig = line;
    } else {
        // For all other declarations stop before the body brace.
        const braceIdx = raw.indexOf('{');
        if (braceIdx > 0) {
            sig = raw.slice(0, braceIdx);
        } else {
            const nlIdx = raw.indexOf('\n');
            sig = nlIdx > 0 ? raw.slice(0, nlIdx) : raw;
        }
    }

    return sig.replace(/\s+/g, ' ').trim().slice(0, 200);
}

/** True when the node carries an `export` modifier keyword. */
function isExported(node: ts.Node): boolean {
    if (!ts.canHaveModifiers(node)) return false;
    return ts.getModifiers(node)?.some(
        m => m.kind === ts.SyntaxKind.ExportKeyword,
    ) ?? false;
}

function extractFromTypeScript(content: string, filePath: string): ExtractedSymbols {
    const src = ts.createSourceFile(
        path.basename(filePath),   // filename — determines JSX/TSX handling
        content,
        ts.ScriptTarget.Latest,
        /* setParentNodes */ false, // not needed for top-down traversal
        tsScriptKind(filePath),
    );

    const imports: string[] = [];
    const symbols: FileSymbol[] = [];

    for (const stmt of src.statements) {

        // ---- Imports ----------------------------------------------------------
        if (ts.isImportDeclaration(stmt)) {
            imports.push(stmt.getText(src).trim());
            continue;
        }

        // ---- Function declarations --------------------------------------------
        if (ts.isFunctionDeclaration(stmt)) {
            symbols.push({
                name:      stmt.name?.text ?? '<anonymous>',
                kind:      'function',
                signature: signatureOf(stmt, src),
                range:     toRange(stmt, src),
            });
            continue;
        }

        // ---- Class declarations -----------------------------------------------
        if (ts.isClassDeclaration(stmt)) {
            symbols.push({
                name:      stmt.name?.text ?? '<anonymous>',
                kind:      'class',
                signature: signatureOf(stmt, src),
                range:     toRange(stmt, src),
            });
            continue;
        }

        // ---- Interface declarations -------------------------------------------
        if (ts.isInterfaceDeclaration(stmt)) {
            symbols.push({
                name:      stmt.name.text,
                kind:      'interface',
                signature: signatureOf(stmt, src),
                range:     toRange(stmt, src),
            });
            continue;
        }

        // ---- Type aliases -----------------------------------------------------
        if (ts.isTypeAliasDeclaration(stmt)) {
            symbols.push({
                name:      stmt.name.text,
                kind:      'type',
                signature: signatureOf(stmt, src),
                range:     toRange(stmt, src),
            });
            continue;
        }

        // ---- Enum declarations ------------------------------------------------
        if (ts.isEnumDeclaration(stmt)) {
            symbols.push({
                name:      stmt.name.text,
                kind:      'enum',
                signature: signatureOf(stmt, src),
                range:     toRange(stmt, src),
            });
            continue;
        }

        // ---- Exported variable statements ------------------------------------
        // Only exported variables are worth indexing at the top level;
        // unexported module-level `const x = 1` are typically internal.
        if (ts.isVariableStatement(stmt) && isExported(stmt)) {
            for (const decl of stmt.declarationList.declarations) {
                if (!ts.isIdentifier(decl.name)) continue;
                const isFunc =
                    decl.initializer !== undefined && (
                        ts.isArrowFunction(decl.initializer) ||
                        ts.isFunctionExpression(decl.initializer)
                    );
                symbols.push({
                    name:      decl.name.text,
                    kind:      isFunc ? 'function' : 'variable',
                    signature: signatureOf(stmt, src),
                    range:     toRange(stmt, src),
                });
            }
        }
    }

    return { imports, symbols };
}

// ---------------------------------------------------------------------------
// Python extractor (regex-based)
// ---------------------------------------------------------------------------

function extractFromPython(content: string): ExtractedSymbols {
    const lines   = content.split('\n');
    const imports: string[] = [];
    const symbols: FileSymbol[] = [];

    // Anchored at line start — top-level declarations have no leading whitespace.
    const importRe = /^(?:import|from)\s+\S/;
    const funcRe   = /^(?:async\s+)?def\s+(\w+)\s*\(([^)]*)\)(?:\s*->\s*[^:]+)?:/;
    const classRe  = /^class\s+(\w+)(?:\([^)]*\))?:/;

    for (let i = 0; i < lines.length; i++) {
        const line = lines[i];

        // Skip blank lines and indented lines (not top-level).
        if (!line.trim() || /^\s/.test(line)) continue;

        if (importRe.test(line)) {
            imports.push(line.trim());
            continue;
        }

        const funcMatch = funcRe.exec(line);
        if (funcMatch) {
            const endLine = _findPythonBlockEnd(lines, i);
            symbols.push({
                name:      funcMatch[1],
                kind:      'function',
                // Drop the trailing colon from the signature.
                signature: line.trim().replace(/:$/, '').trim().slice(0, 200),
                range: {
                    start: { line: i, character: 0 },
                    end:   { line: endLine, character: lines[endLine].length },
                },
            });
            continue;
        }

        const classMatch = classRe.exec(line);
        if (classMatch) {
            const endLine = _findPythonBlockEnd(lines, i);
            symbols.push({
                name:      classMatch[1],
                kind:      'class',
                signature: line.trim().replace(/:$/, '').trim().slice(0, 200),
                range: {
                    start: { line: i, character: 0 },
                    end:   { line: endLine, character: lines[endLine].length },
                },
            });
        }
    }

    return { imports, symbols };
}

/**
 * Starting from the header line of a top-level Python `def`/`class`,
 * scan forward to find the last line of the indented block body.
 *
 * An indented block ends when a subsequent line has no leading whitespace
 * (i.e. returns to the top level) or at EOF.  Blank lines inside the
 * block are considered part of the body.
 */
function _findPythonBlockEnd(lines: string[], headerLine: number): number {
    let blockEnd = headerLine;
    for (let j = headerLine + 1; j < lines.length; j++) {
        const nextLine = lines[j];
        // Empty / whitespace-only lines are part of the block body.
        if (!nextLine.trim()) { blockEnd = j; continue; }
        // A line with no leading whitespace means we've left the block.
        if (!/^\s/.test(nextLine)) break;
        blockEnd = j;
    }
    return blockEnd;
}

// ---------------------------------------------------------------------------
// Java extractor (regex-based)
// ---------------------------------------------------------------------------

// Java keywords that can appear as a "name" in the method regex but are
// not actually method names (control-flow, etc.).
const JAVA_KEYWORDS = new Set([
    'if', 'else', 'for', 'while', 'do', 'switch', 'case',
    'return', 'throw', 'new', 'catch', 'finally', 'try',
    'assert', 'break', 'continue', 'instanceof',
]);

function extractFromJava(content: string): ExtractedSymbols {
    const lines   = content.split('\n');
    const imports: string[] = [];
    const symbols: FileSymbol[] = [];

    const importRe = /^import\s+(?:static\s+)?[\w.*]+;/;
    // class / interface / enum with optional access + modifier keywords
    const typeRe   = /^(?:(?:public|private|protected|abstract|final|static)\s+)*(?:(class|interface|enum)\s+(\w+))/;
    // Method: at least one access/modifier keyword, then return type + name + (
    const methodRe = /^(?:(?:public|private|protected|static|final|abstract|synchronized|native)\s+)+(?:<[^>]*>\s+)?(?:[\w$[\]<>,? ]+\s+)?(\w+)\s*\([^;]*\)\s*(?:throws\s+[\w,\s]+)?\s*[{;]/;

    for (let i = 0; i < lines.length; i++) {
        const trimmed = lines[i].trim();
        if (!trimmed || trimmed.startsWith('//') || trimmed.startsWith('*')) continue;

        if (importRe.test(trimmed)) {
            imports.push(trimmed);
            continue;
        }

        const typeMatch = typeRe.exec(trimmed);
        if (typeMatch) {
            const declKind = typeMatch[1] as 'class' | 'interface' | 'enum';
            const kind: SymbolKind =
                declKind === 'interface' ? 'interface' :
                declKind === 'enum'      ? 'enum'      : 'class';
            symbols.push({
                name:      typeMatch[2],
                kind,
                signature: trimmed.replace(/\s*\{.*$/, '').trim().slice(0, 200),
                range: {
                    start: { line: i, character: 0 },
                    end:   { line: i, character: lines[i].length },
                },
            });
            continue;
        }

        const methodMatch = methodRe.exec(trimmed);
        if (methodMatch) {
            const name = methodMatch[1];
            if (JAVA_KEYWORDS.has(name)) continue;
            symbols.push({
                name,
                kind:      'function',
                signature: trimmed.replace(/\s*\{.*$/, '').replace(/;$/, '').trim().slice(0, 200),
                range: {
                    start: { line: i, character: 0 },
                    end:   { line: i, character: lines[i].length },
                },
            });
        }
    }

    return { imports, symbols };
}
