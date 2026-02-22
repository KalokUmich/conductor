/**
 * ContextGatherer — enriches a code snippet with surrounding workspace context
 * before sending it to the backend /context/explain endpoint.
 *
 * Inspired by Augment Code's approach: rather than sending only the selected
 * lines, we attach:
 *   1. The full file content (so the backend can find the containing function,
 *      imports, class hierarchy, etc.)
 *   2. A configurable window of lines around the selection (immediate context)
 *   3. Top-level imports extracted from the file
 *   4. The containing function / class signature (via LSP if available)
 *   5. Related file snippets found via LSP "find references" or filename search
 *
 * All gathering is best-effort: failures are silently swallowed and the
 * corresponding field is left as undefined.  The backend ContextEnricher
 * handles missing fields gracefully.
 */
import * as vscode from 'vscode';
import { RagClient, RagSearchResultItem } from './ragClient';

// ---------------------------------------------------------------------------
// Public interface
// ---------------------------------------------------------------------------

export interface GatherInput {
    /** The selected code text. */
    code: string;
    /** Workspace-relative path of the file. */
    relativePath: string;
    /** 1-based start line of the selection. */
    startLine: number;
    /** 1-based end line of the selection. */
    endLine: number;
    /** VS Code language ID (e.g. "python", "typescript"). */
    language: string;
}

export interface ContextBundle {
    /** Full content of the source file (may be truncated for very large files). */
    fileContent?: string;
    /** Lines immediately before/after the selection for quick context. */
    surroundingCode?: string;
    /** Import / require statements extracted from the file. */
    imports: string[];
    /** Signature of the enclosing function or class (if resolvable). */
    containingFunction?: string;
    /** Key snippets from related files (LSP definitions or filename matches). */
    relatedFiles: RelatedFileSnippet[];
}

export interface RelatedFileSnippet {
    relativePath: string;
    /** Short excerpt (up to 30 lines) of the most relevant section. */
    snippet: string;
    /** Reason this file was included. */
    reason: 'definition' | 'reference' | 'import' | 'filename_match';
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Lines of context to capture above/below the selection. */
const CONTEXT_LINES = 15;

/** Maximum characters for the full file content sent to the backend. */
const MAX_FILE_CHARS = 40_000;

/** Maximum number of related files to attach. */
const MAX_RELATED = 3;

/** Maximum lines per related file snippet. */
const RELATED_SNIPPET_LINES = 30;

// ---------------------------------------------------------------------------
// ContextGatherer class
// ---------------------------------------------------------------------------

export class ContextGatherer {
    private readonly _ragClient?: RagClient;
    private readonly _workspaceId?: string;

    /**
     * @param ragClient   Optional RagClient for semantic search augmentation.
     * @param workspaceId Workspace/room ID required when ragClient is provided.
     */
    constructor(ragClient?: RagClient, workspaceId?: string) {
        this._ragClient = ragClient;
        this._workspaceId = workspaceId;
    }

    /**
     * Gather context for the given code selection.
     * All steps are best-effort; failures do not throw.
     */
    async gather(input: GatherInput): Promise<ContextBundle> {
        const bundle: ContextBundle = { imports: [], relatedFiles: [] };

        const fileUri = await this._resolveFileUri(input.relativePath);
        if (!fileUri) return bundle;

        // 1. Full file content (capped)
        bundle.fileContent = await this._readFile(fileUri);

        // 2. Surrounding code window
        if (bundle.fileContent) {
            bundle.surroundingCode = this._extractWindow(
                bundle.fileContent,
                input.startLine,
                input.endLine,
            );

            // 3. Imports
            bundle.imports = this._extractImports(bundle.fileContent, input.language);

            // 4. Containing function via simple heuristic
            bundle.containingFunction = this._findContainingFunction(
                bundle.fileContent,
                input.startLine,
                input.language,
            );
        }

        // 5. Related files via LSP (best-effort)
        bundle.relatedFiles = await this._gatherRelatedFiles(
            fileUri,
            input.startLine,
            input.endLine,
            input.relativePath,
        );

        return bundle;
    }

    // -----------------------------------------------------------------------
    // Private helpers
    // -----------------------------------------------------------------------

    private async _resolveFileUri(relativePath: string): Promise<vscode.Uri | undefined> {
        const folders = vscode.workspace.workspaceFolders;
        if (!folders) return undefined;

        for (const folder of folders) {
            const candidate = vscode.Uri.joinPath(folder.uri, relativePath);
            try {
                await vscode.workspace.fs.stat(candidate);
                return candidate;
            } catch { /* try next */ }
        }
        return undefined;
    }

    private async _readFile(uri: vscode.Uri): Promise<string | undefined> {
        try {
            const doc = await vscode.workspace.openTextDocument(uri);
            const text = doc.getText();
            // Cap to avoid oversized payloads
            return text.length > MAX_FILE_CHARS ? text.slice(0, MAX_FILE_CHARS) + '\n// [truncated]' : text;
        } catch {
            return undefined;
        }
    }

    /**
     * Extract a window of lines around the selection.
     * Returns lines [startLine - CONTEXT_LINES, endLine + CONTEXT_LINES]
     * with 1-based line numbers as comments prepended.
     */
    private _extractWindow(fileContent: string, startLine: number, endLine: number): string {
        const lines = fileContent.split('\n');
        const from = Math.max(0, startLine - 1 - CONTEXT_LINES);
        const to   = Math.min(lines.length, endLine + CONTEXT_LINES);
        return lines
            .slice(from, to)
            .map((l, i) => `${from + i + 1}: ${l}`)
            .join('\n');
    }

    /**
     * Extract import / require / use statements from the file.
     */
    private _extractImports(fileContent: string, language: string): string[] {
        const lines = fileContent.split('\n');
        const importLines: string[] = [];

        const patterns: Record<string, RegExp> = {
            python:     /^(?:import |from )\S/,
            typescript: /^(?:import |const .+ = require)/,
            javascript: /^(?:import |const .+ = require)/,
            java:       /^import /,
            go:         /^import /,
        };

        const pattern = patterns[language] ?? /^(?:import |from |require|use )/;

        for (const line of lines) {
            if (importLines.length >= 30) break;
            if (pattern.test(line.trim())) {
                importLines.push(line.trim());
            }
        }
        return importLines;
    }

    /**
     * Walk backwards from the selection start to find the enclosing function
     * or class declaration.  Uses simple line-pattern matching — fast but
     * language-aware.
     */
    private _findContainingFunction(
        fileContent: string,
        startLine: number,
        language: string,
    ): string | undefined {
        const lines = fileContent.split('\n');
        const scanFrom = Math.min(startLine - 1, lines.length - 1);

        // Patterns that signal a function / class / method definition
        const defPatterns: Record<string, RegExp> = {
            python:     /^\s*(?:def |class |async def )/,
            typescript: /^\s*(?:function |class |async function |(?:public|private|protected|static).*\(|const \w+ = (?:async\s*)?\()/,
            javascript: /^\s*(?:function |class |async function |const \w+ = (?:async\s*)?\()/,
            java:       /^\s*(?:public|private|protected|static|void|\w+)\s+\w+\s*\(/,
            go:         /^\s*func /,
        };
        const pattern = defPatterns[language] ?? /^\s*(?:function |def |class |func )/;

        for (let i = scanFrom; i >= 0; i--) {
            if (pattern.test(lines[i])) {
                return lines[i].trim().slice(0, 120);
            }
        }
        return undefined;
    }

    /**
     * Use VS Code's LSP commands to find definitions / references for symbols
     * near the selection midpoint, then attach short snippets from those files.
     */
    private async _gatherRelatedFiles(
        fileUri: vscode.Uri,
        startLine: number,
        endLine: number,
        ownRelativePath: string,
    ): Promise<RelatedFileSnippet[]> {
        const results: RelatedFileSnippet[] = [];

        // Use the midpoint of the selection as the target position
        const midLine = Math.floor((startLine + endLine) / 2) - 1;
        const position = new vscode.Position(midLine, 0);

        // Try LSP go-to-definition
        try {
            const defs = await vscode.commands.executeCommand<vscode.Location[]>(
                'vscode.executeDefinitionProvider', fileUri, position,
            );
            for (const def of (defs ?? []).slice(0, MAX_RELATED)) {
                const rel = vscode.workspace.asRelativePath(def.uri);
                if (rel === ownRelativePath) continue;  // skip self
                const snippet = await this._readSnippetAt(def.uri, def.range.start.line);
                if (snippet) {
                    results.push({ relativePath: rel, snippet, reason: 'definition' });
                }
                if (results.length >= MAX_RELATED) return results;
            }
        } catch { /* LSP unavailable */ }

        // Try LSP find-references (limited to 2 files)
        if (results.length < MAX_RELATED) {
            try {
                const refs = await vscode.commands.executeCommand<vscode.Location[]>(
                    'vscode.executeReferenceProvider', fileUri, position,
                );
                const seen = new Set(results.map(r => r.relativePath));
                for (const ref of (refs ?? []).slice(0, 6)) {
                    const rel = vscode.workspace.asRelativePath(ref.uri);
                    if (rel === ownRelativePath || seen.has(rel)) continue;
                    seen.add(rel);
                    const snippet = await this._readSnippetAt(ref.uri, ref.range.start.line);
                    if (snippet) {
                        results.push({ relativePath: rel, snippet, reason: 'reference' });
                    }
                    if (results.length >= MAX_RELATED) return results;
                }
            } catch { /* LSP unavailable */ }
        }

        // Augment with backend RAG semantic search (best-effort)
        if (results.length < MAX_RELATED && this._ragClient && this._workspaceId) {
            try {
                const seen = new Set(results.map(r => r.relativePath));
                seen.add(ownRelativePath);

                // Use the file content around the selection as the search query
                const doc = await vscode.workspace.openTextDocument(fileUri);
                const queryStart = Math.max(0, startLine - 1 - 5);
                const queryEnd = Math.min(doc.lineCount, endLine + 5);
                const queryLines: string[] = [];
                for (let i = queryStart; i < queryEnd; i++) {
                    queryLines.push(doc.lineAt(i).text);
                }
                const query = queryLines.join('\n');

                const ragResponse = await this._ragClient.search(
                    this._workspaceId, query, MAX_RELATED * 2,
                );
                for (const item of ragResponse.results) {
                    if (seen.has(item.file_path)) continue;
                    seen.add(item.file_path);

                    // Read the snippet from the file at the indicated lines
                    const ragUri = await this._resolveFileUri(item.file_path);
                    if (!ragUri) continue;
                    const snippet = await this._readSnippetAt(ragUri, item.start_line - 1);
                    if (snippet) {
                        results.push({
                            relativePath: item.file_path,
                            snippet,
                            reason: 'definition', // semantic match treated as definition
                        });
                    }
                    if (results.length >= MAX_RELATED) break;
                }
            } catch {
                // RAG unavailable — continue without
            }
        }

        return results;
    }

    /** Read RELATED_SNIPPET_LINES lines starting at the given 0-based line. */
    private async _readSnippetAt(uri: vscode.Uri, line: number): Promise<string | undefined> {
        try {
            const doc = await vscode.workspace.openTextDocument(uri);
            const from = Math.max(0, line - 3);
            const to   = Math.min(doc.lineCount, from + RELATED_SNIPPET_LINES);
            const lines: string[] = [];
            for (let i = from; i < to; i++) {
                lines.push(doc.lineAt(i).text);
            }
            return lines.join('\n');
        } catch {
            return undefined;
        }
    }
}
