/**
 * Conductor "Explain with Context" pipeline.
 *
 * Orchestrates eight stages to produce an LLM explanation of a code
 * selection, enriched with LSP definitions, ranked related files, and
 * (optionally) semantic embeddings.
 *
 * Stage summary
 * -------------
 *   1. Selection    – received from the caller (already resolved)
 *   2. LSP context  – definition + references via VS Code LSP commands
 *   3. Ranking      – hybrid structural + semantic relevance scoring
 *   4. Context plan – deduplicated read-file operations
 *   5. Execute plan – read file slices via VS Code workspace API
 *   6. XML prompt   – assemble all snippets into a structured XML string
 *   7. LLM call     – POST /api/context/explain-rich (agentic: backend explores codebase with tools)
 *   8. Response     – return explanation to the caller for rendering
 *
 * Graceful degradation
 * --------------------
 *   - If the LSP command throws or returns nothing, ranking falls back
 *     to structural-only with the current file only.
 *   - If the embedding endpoint is unavailable or the local vector index
 *     is empty, semantic results are silently replaced with [].
 *   - If reading a related file fails, that file is simply omitted.
 *   - Every stage is wrapped in a try/catch; failures are logged with
 *     timing information so the caller always receives a result.
 *
 * @module services/explainWithContextPipeline
 */

import * as path from 'path';

// Type-only VS Code import — erased at compile time, never a top-level require.
import type * as vscodeT from 'vscode';

import { resolveLspContext }                                   from './lspResolver';
import { rank, RankInput, RankOptions }                       from './relevanceRanker';
import { buildContextPlan, ReadFileOp }                       from './contextPlanGenerator';
import { assembleXmlPrompt, FileSnippet, ProjectMetadataInput } from './xmlPromptAssembler';
import { collectProjectMetadata, ProjectMetadata }             from './projectMetadataCollector';
import { extractSymbols }                                     from './symbolExtractor';
import { ConductorDb }                                        from './conductorDb';
import {
    WorkspaceConfig, DEFAULT_WORKSPACE_CONFIG,
} from './workspaceStorage';

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

/** Progress event emitted during the pipeline for real-time UI feedback. */
export interface PipelineProgressEvent {
    /** 'pipeline' for extension-side stages, 'agent' for backend agent loop, 'complete' when done. */
    phase:    'pipeline' | 'agent' | 'complete';
    /** Human-readable description of what's happening right now. */
    message:  string;
    /** Agent event kind (thinking / tool_call / tool_result / done / error). */
    kind?:    string;
    /** Extra details (e.g. tool name, params, iteration count). */
    detail?:  Record<string, any>;
}

export interface PipelineInput {
    // ---- Selection (Stage 1) ------------------------------------------------
    uri:               vscodeT.Uri;
    /** Cursor / active position inside the selection for LSP queries. */
    selectionPosition: vscodeT.Position;
    /** Workspace-relative path of the file containing the selection. */
    relativePath:      string;
    /** VS Code language ID, e.g. "typescript". */
    language:          string;
    /** The selected code text (raw). */
    code:              string;
    /** 1-based start line of the selection. */
    startLine:         number;
    /** 1-based end line of the selection. */
    endLine:           number;

    // ---- Context ------------------------------------------------------------
    /** User question; defaults to "Explain this code". */
    question?:         string;
    backendUrl:        string;
    /** Workspace / room ID passed to the backend for RAG search augmentation. */
    workspaceId?:      string;
    conductorDb:       ConductorDb | null;
    workspaceFolders:  vscodeT.WorkspaceFolder[];

    // ---- Tuning (sourced from config at runtime) ----------------------------
    /**
     * Extension-side workspace settings (`.conductor/config.json`).
     * Controls ranking caps and semantic top-K.
     * Falls back to `DEFAULT_WORKSPACE_CONFIG` for any missing field.
     */
    workspaceConfig?: WorkspaceConfig;

    // ---- Progress -----------------------------------------------------------
    /** Optional callback for real-time progress updates. */
    onProgress?: (event: PipelineProgressEvent) => void;
}

export interface PipelineOutput {
    explanation: string;
    model:       string;
    /** The assembled XML prompt that was sent to the LLM. */
    xmlPrompt:   string;
    /** Wall-clock milliseconds per stage, keyed by stage name. */
    timings:     Record<string, number>;
    /** Structured explanation fields parsed by the backend (if available). */
    structured?: Record<string, string>;
    /** Agent thinking steps (tool calls, reasoning) for debugging. */
    thinking_steps: ThinkingStep[];
}

// ---------------------------------------------------------------------------
// Pipeline entry point
// ---------------------------------------------------------------------------

const LOG = '[ExplainPipeline]';

/**
 * Run the full "Explain with Context" pipeline and return the LLM response.
 *
 * Every stage is individually guarded; failures produce a warning log and a
 * safe default, so the pipeline always completes (possibly with reduced context).
 */
export async function runExplainPipeline(input: PipelineInput): Promise<PipelineOutput> {
    // Deferred require — only executed at VS Code runtime, never during tests.
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const vscode = require('vscode') as typeof vscodeT;

    const timings: Record<string, number> = {};
    const question = input.question ?? `Explain this ${input.language} code.`;

    const progress = input.onProgress;

    console.log(`${LOG} === Pipeline start ===`);
    console.log(`${LOG} file=${input.relativePath} lines=${input.startLine}-${input.endLine} lang=${input.language}`);
    console.log(`${LOG} conductorDb=${input.conductorDb ? 'SET' : 'NULL'}`);
    console.log(`${LOG} workspaceConfig=${input.workspaceConfig ? JSON.stringify(input.workspaceConfig) : 'NONE (using defaults)'}`);
    console.log(`${LOG} workspaceFolders=${input.workspaceFolders.length} backendUrl=${input.backendUrl}`);

    progress?.({ phase: 'pipeline', message: 'Gathering context...' });

    // Resolve workspace tuning (ranking caps, topK) from .conductor/config.json.
    const cfg = { ...DEFAULT_WORKSPACE_CONFIG, ...(input.workspaceConfig ?? {}) };
    console.log(`${LOG} Resolved cfg: maxRelated=${cfg.maxRelated} maxContextFiles=${cfg.maxContextFiles} semanticTopK=${cfg.semanticTopK}`);

    // -------------------------------------------------------------------------
    // Stage 2 — LSP context
    // -------------------------------------------------------------------------
    let lspResult: { definition?: { path: string; range: { start: { line: number; character: number }; end: { line: number; character: number } } }; references: Array<{ path: string; range: { start: { line: number; character: number }; end: { line: number; character: number } } }> } = { references: [] };
    {
        const t0 = performance.now();
        try {
            lspResult = await resolveLspContext(input.uri, input.selectionPosition);
            console.log(
                `${LOG} [2/8] LSP: def=${lspResult.definition ? 'yes' : 'no'} refs=${lspResult.references.length}`,
            );
        } catch (err) {
            console.log(`${LOG} [2/8] LSP unavailable — falling back:`, err);
        }
        timings['lsp'] = performance.now() - t0;
        console.log(`${LOG} Stage 2 (LSP context): ${timings['lsp'].toFixed(1)}ms`);
    }

    // -------------------------------------------------------------------------
    // Stage 2.5 — Full current file content (always included as context)
    // -------------------------------------------------------------------------
    let fullFileContent: string | undefined;
    {
        const t0 = performance.now();
        try {
            let fileUri: vscodeT.Uri | undefined;
            for (const folder of input.workspaceFolders) {
                const candidate = vscode.Uri.joinPath(folder.uri, input.relativePath);
                try {
                    await vscode.workspace.fs.stat(candidate);
                    fileUri = candidate;
                    break;
                } catch { /* try next */ }
            }
            if (fileUri) {
                const doc = await vscode.workspace.openTextDocument(fileUri);
                fullFileContent = doc.getText();
                // Cap the full-file context at 60 KB to stay within the XML budget.
                if (fullFileContent.length > 60_000) {
                    const cutAt = fullFileContent.lastIndexOf('\n', 60_000);
                    fullFileContent = fullFileContent.slice(0, cutAt > 0 ? cutAt : 60_000)
                        + '\n… [file truncated]';
                }
            }
        } catch (err) {
            console.log(`${LOG} Stage 2.5 (full file) failed (non-fatal):`, err);
        }
        const elapsed = performance.now() - t0;
        console.log(`${LOG} Stage 2.5 (full file): ${elapsed.toFixed(1)}ms — ${fullFileContent?.length ?? 0} chars`);
    }

    // -------------------------------------------------------------------------
    // Stage 2.6 — Import neighbours (resolve imports → workspace paths)
    // -------------------------------------------------------------------------
    let importNeighbors: string[] = [];
    {
        const t0 = performance.now();
        try {
            const extracted = extractSymbols(
                input.workspaceFolders.length > 0
                    ? path.join(input.workspaceFolders[0].uri.fsPath, input.relativePath)
                    : input.relativePath,
            );
            importNeighbors = _resolveImportPaths(
                extracted.imports,
                input.relativePath,
                input.language,   // enables Python absolute import resolution
            );
        } catch (err) {
            console.log(`${LOG} Import resolution failed (non-fatal):`, err);
        }
        const elapsed = performance.now() - t0;
        console.log(`${LOG} Stage 2.6 (import neighbours): ${elapsed.toFixed(1)}ms — found ${importNeighbors.length}`);
    }

    progress?.({ phase: 'pipeline', message: 'Resolving dependencies...' });

    // -------------------------------------------------------------------------
    // Stage 2.7 — Augment-style dependency resolution
    //
    // 1. Build a dependency plan from the selected code (types, services,
    //    method calls, constants) with import-aware strategy assignment.
    // 2. Resolve ALL dependencies in parallel (DB lookup / file read / semantic).
    // 3. Detect unresolved deps and issue targeted follow-up queries.
    // 4. Emergency fallback: single broad query if > 50% unresolved AND DB empty.
    //
    // This replaces the old single-query semantic search and type-name-only
    // extraction with the targeted multi-query approach used by Augment Code.
    // -------------------------------------------------------------------------
    const typeDefinitionSnippets: Array<{ path: string; content: string }> = [];
    {
        const t0 = performance.now();
        try {
            // Step 1: Build dependency plan from the selected code.
            const depNodes = _buildDependencyPlan(
                input.code, input.language, fullFileContent,
                importNeighbors, input.conductorDb,
            );
            console.log(
                `${LOG} [2.7] Dependencies identified: ${depNodes.map(d => d.name).join(', ')}`,
            );

            // Step 2: Resolve all dependencies in parallel (3 rounds).
            const resolved = await _resolveAllDependencies(
                depNodes, input.conductorDb, input.workspaceFolders, vscode,
            );

            // Step 3: Collect results into the format downstream stages expect.
            for (const [, result] of resolved) {
                typeDefinitionSnippets.push({ path: result.path, content: result.content });
                // Add resolved paths to import neighbors for ranker graph-boost.
                if (result.path && !importNeighbors.includes(result.path)) {
                    importNeighbors.push(result.path);
                }
            }

            console.log(
                `${LOG} [2.7] Resolved: ${resolved.size}/${depNodes.length} deps, ` +
                `${typeDefinitionSnippets.length} snippets`,
            );
        } catch (err) {
            console.log(`${LOG} Stage 2.7 (dependency resolution) failed (non-fatal):`, err);
        }
        timings['deps'] = performance.now() - t0;
        console.log(`${LOG} Stage 2.7 (dependency resolution): ${timings['deps'].toFixed(1)}ms`);
    }

    // -------------------------------------------------------------------------
    // Stage 3 — Rank context
    // -------------------------------------------------------------------------
    const t3 = performance.now();
    const rankInput: RankInput = {
        currentFile:     input.relativePath,
        lsp:             lspResult,
        importNeighbors,
        semanticResults: [],
    };
    const rankOpts: RankOptions = {
        maxReferences: cfg.maxRelated,
        maxFiles:      cfg.maxContextFiles,
    };
    const ranked = rank(rankInput, rankOpts);
    timings['ranking'] = performance.now() - t3;
    console.log(
        `${LOG} Stage 3 (ranking): ${timings['ranking'].toFixed(1)}ms — ${ranked.length} item(s)`,
    );

    // -------------------------------------------------------------------------
    // Stage 4 — Generate context plan
    // -------------------------------------------------------------------------
    const t4 = performance.now();
    const plan = buildContextPlan(ranked);
    timings['plan'] = performance.now() - t4;
    console.log(
        `${LOG} Stage 4 (context plan): ${timings['plan'].toFixed(1)}ms — ${plan.length} file op(s)`,
    );

    // -------------------------------------------------------------------------
    // Stage 5 — Execute plan (read file slices)
    // -------------------------------------------------------------------------
    const t5 = performance.now();
    const fileContents = await _executePlan(plan, input.workspaceFolders, vscode);
    timings['read_files'] = performance.now() - t5;
    console.log(
        `${LOG} Stage 5 (read files): ${timings['read_files'].toFixed(1)}ms — read ${fileContents.size} file(s)`,
    );

    // -------------------------------------------------------------------------
    // Stage 5.5 — Project metadata (cached, <1ms after first call)
    // -------------------------------------------------------------------------
    let projectMetadata: ProjectMetadata | null = null;
    {
        const t55 = performance.now();
        try {
            projectMetadata = await collectProjectMetadata(input.workspaceFolders);
            if (projectMetadata) {
                console.log(
                    `${LOG} Stage 5.5 (project metadata): name=${projectMetadata.name} ` +
                    `langs=${projectMetadata.languages.length} frameworks=${projectMetadata.frameworks.length}`,
                );
            }
        } catch (err) {
            console.log(`${LOG} Stage 5.5 (project metadata) failed (non-fatal):`, err);
        }
        timings['project_metadata'] = performance.now() - t55;
        console.log(`${LOG} Stage 5.5 (project metadata): ${timings['project_metadata'].toFixed(1)}ms`);
    }

    progress?.({ phase: 'pipeline', message: 'Preparing prompt...' });

    // -------------------------------------------------------------------------
    // Stage 6 — Build XML prompt
    // -------------------------------------------------------------------------
    const t6 = performance.now();
    const xmlInput = _buildXmlInput(
        input,
        lspResult,
        fileContents,
        question,
        fullFileContent,
        typeDefinitionSnippets,
        projectMetadata,
    );
    const { xml: xmlPrompt, wasTrimmed, tokenCount } = assembleXmlPrompt(xmlInput);
    timings['xml_assembly'] = performance.now() - t6;
    if (wasTrimmed) {
        console.log(`${LOG} [6/8] XML prompt was trimmed to fit budget`);
    }
    console.log(
        `${LOG} Stage 6 (XML assembly): ${timings['xml_assembly'].toFixed(1)}ms — ${xmlPrompt.length} chars, ~${tokenCount} tokens`,
    );

    // -------------------------------------------------------------------------
    // Stage 7 — Send to LLM (SSE streaming with progress)
    // -------------------------------------------------------------------------
    progress?.({ phase: 'agent', message: 'AI is exploring the codebase...', kind: 'start' });

    const t7 = performance.now();
    let explanation = '';
    let model = 'ai';
    let structured: Record<string, string> | undefined;
    let thinking_steps: ThinkingStep[] = [];
    try {
        const result = await _callLlm(xmlPrompt, input);
        explanation     = result.explanation;
        model           = result.model;
        structured      = result.structured;
        thinking_steps  = result.thinking_steps;
    } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        console.error(`${LOG} [7/8] LLM call failed:`, msg);
        throw err; // re-throw so the caller can surface the error to the user
    }
    timings['llm'] = performance.now() - t7;
    console.log(
        `${LOG} Stage 7 (LLM call): ${timings['llm'].toFixed(1)}ms — model=${model}`,
    );

    progress?.({ phase: 'complete', message: 'Done' });

    // Stage 8 (render) is handled by the caller after this function returns.
    console.log(`${LOG} Stage 8 (render): delegated to caller`);

    const total = Object.values(timings).reduce((a, b) => a + b, 0);
    console.log(`${LOG} Pipeline complete — total=${total.toFixed(1)}ms`);

    return { explanation, model, xmlPrompt, timings, structured, thinking_steps };
}

// ---------------------------------------------------------------------------
// Private helpers
// ---------------------------------------------------------------------------

/**
 * Best-effort resolution of import statements to workspace-relative file paths.
 *
 * Handles:
 *  - Relative TS/JS:  `import x from './utils'`  → `src/utils.ts`
 *  - Relative Python: `from .models import X`     → `<currentDir>/models.py`
 *  - Absolute Python: `from foo.bar.baz import X` → `foo/bar/baz.py`
 *  - require():       `require('./config')`        → resolved `.ts` / `.js`
 */
// ---------------------------------------------------------------------------
// Augment-style dependency types
// ---------------------------------------------------------------------------

/** A single dependency node in the plan — drives resolution strategy. */
interface DependencyNode {
    name: string;
    kind: 'type' | 'service' | 'function' | 'method_call' | 'constant';
    /** Natural-language query for codebase search. */
    query: string;
    /** For method_call deps: the receiver class name (e.g. "AsyncPolicyService"). */
    receiver?: string;
    /** Known file path from imports or DB pre-check. */
    knownPath?: string;
    /** Resolution strategy chosen during planning. */
    strategy: 'read_file' | 'symbol_lookup';
}

/** A successfully resolved dependency with its content. */
interface ResolvedDependency {
    dep:         DependencyNode;
    path:        string;
    content:     string;
    resolvedVia: 'file_read' | 'symbol_db';
}

/** Builtins & framework names that appear everywhere but rarely need lookup. */
const _DEP_SKIP = new Set([
    // Python builtins / typing
    'None', 'True', 'False', 'Optional', 'Union', 'List', 'Dict', 'Set',
    'Tuple', 'Any', 'Type', 'Callable', 'Iterator', 'Generator', 'Coroutine',
    'Awaitable', 'ClassVar', 'Final', 'Literal', 'Annotated', 'Protocol',
    'TypeVar', 'Generic', 'ABC', 'BaseModel',
    // TypeScript utility types
    'Promise', 'Array', 'Record', 'Partial', 'Required', 'Readonly',
    'Pick', 'Omit', 'Exclude', 'Extract', 'NonNullable', 'ReturnType',
    'InstanceType', 'Parameters', 'ConstructorParameters',
    // Java boxing types
    'String', 'Integer', 'Long', 'Double', 'Float', 'Boolean', 'Object',
    // Framework noise
    'Security', 'Depends', 'Query', 'Header', 'Path', 'Body',
    'Response', 'Request', 'HTTPException', 'HTTPStatus', 'APIRouter',
]);

// ---------------------------------------------------------------------------
// _buildDependencyPlan — Augment-style dependency extraction
// ---------------------------------------------------------------------------

/**
 * Analyse selected code and build a dependency plan with strategy assignments.
 *
 * Improvements over the old `_extractDependencies`:
 *   1. **Import-aware strategy**: checks if a type is imported from a known
 *      module and assigns `read_file` strategy with the known path.
 *   2. **Method-call decomposition**: for `service.method(...)`, resolves the
 *      service's type annotation from fullFileContent and creates deps for
 *      both the class and the specific method.
 *   3. **DB pre-check**: if a symbol exists in the DB, assigns `symbol_lookup`.
 *   4. **Priority ordering**: params/return types first, then method calls,
 *      then constants/decorators.
 */
function _buildDependencyPlan(
    code:            string,
    language:        string | undefined,
    fullFileContent: string | undefined,
    importPaths:     string[],
    db:              ConductorDb | null,
): DependencyNode[] {
    const nodes: DependencyNode[] = [];
    const seen = new Set<string>();

    // Build a map of import module paths for strategy assignment.
    // importPaths are already resolved workspace-relative paths.
    const importPathSet = new Set(importPaths);

    const add = (
        name: string,
        kind: DependencyNode['kind'],
        opts?: { query?: string; receiver?: string; knownPath?: string },
    ) => {
        if (seen.has(name) || _DEP_SKIP.has(name) || name.length <= 2) return;
        seen.add(name);

        const query = opts?.query ?? _defaultQuery(name, kind, language);

        // Strategy assignment: known path → read_file, DB hit → symbol_lookup, else skip.
        let strategy: DependencyNode['strategy'] = 'symbol_lookup';
        let knownPath = opts?.knownPath;

        // Check DB for a direct symbol match.
        if (!knownPath && db) {
            const dbSyms = db.getSymbolsByName(name);
            if (dbSyms.length > 0) {
                strategy = 'symbol_lookup';
                knownPath = dbSyms[0].path;
            }
        }

        // If we have a known path (from imports or DB), prefer file read.
        if (knownPath) {
            strategy = importPathSet.has(knownPath) ? 'read_file' : 'symbol_lookup';
        }

        nodes.push({ name, kind, query, receiver: opts?.receiver, knownPath, strategy });
    };

    // --- Phase 1: PascalCase type annotations (params, return, generics) ---
    const TYPE_RE = /(?:[:,>)\s])([A-Z][A-Za-z0-9_]{2,})(?![a-z])/g;
    let m: RegExpExecArray | null;
    while ((m = TYPE_RE.exec(code)) !== null) add(m[1], 'type');

    // --- Phase 2: @inject / @Inject decorator arguments ---
    const INJECT_RE = /@inject\s*\(([^)]+)\)/gi;
    while ((m = INJECT_RE.exec(code)) !== null) {
        for (const part of m[1].split(',')) {
            const name = part.trim();
            if (/^[A-Z]/.test(name)) add(name, 'service', { query: `${name} class implementation` });
        }
    }

    // --- Phase 3: Method-call decomposition ---
    // For variable.method_name(...), resolve the variable's type annotation
    // from fullFileContent and create deps for both the class and method.
    const METHOD_RE = /(\w+)\.(\w{3,})\s*\(/g;
    while ((m = METHOD_RE.exec(code)) !== null) {
        const [, obj, method] = m;
        if (/^(log|self|this|console|Math|JSON|Array|Object)$/.test(obj)) continue;

        // Try to resolve the receiver's type from the full file content.
        let receiverType: string | undefined;
        if (fullFileContent) {
            // Python: `obj: TypeName` or `obj : TypeName`
            const pyAnnot = new RegExp(`${obj}\\s*:\\s*([A-Z][A-Za-z0-9_]+)`);
            const pyMatch = pyAnnot.exec(fullFileContent);
            if (pyMatch) receiverType = pyMatch[1];

            // TypeScript: `obj: TypeName` or `private obj: TypeName`
            if (!receiverType) {
                const tsAnnot = new RegExp(`${obj}\\s*:\\s*([A-Z][A-Za-z0-9_<>]+)`);
                const tsMatch = tsAnnot.exec(fullFileContent);
                if (tsMatch) receiverType = tsMatch[1].replace(/<.*>$/, ''); // strip generics
            }
        }

        // Add the receiver class as a dep if found.
        if (receiverType) {
            add(receiverType, 'service', { query: `${receiverType} class implementation` });
        }

        // Add the method itself with the receiver reference.
        const methodQuery = receiverType
            ? `${receiverType} ${method} method implementation`
            : `${method} method implementation`;
        add(method, 'method_call', { query: methodQuery, receiver: receiverType });
    }

    // --- Phase 4: UPPER_CASE constants ---
    const CONST_RE = /\b([A-Z][A-Z_]{3,})\b/g;
    while ((m = CONST_RE.exec(code)) !== null) {
        const name = m[1];
        if (!/^(HTTP|URL|PREFIX|SCOPE|GET|POST|PUT|DELETE|OK|TRUE|FALSE|NONE|NULL)$/.test(name)) {
            add(name, 'constant', { query: `${name} constant definition` });
        }
    }

    return nodes;
}

/** Generate a natural-language search query for a dependency. */
function _defaultQuery(name: string, kind: string, language?: string): string {
    const lang = language ?? 'code';
    switch (kind) {
        case 'type':        return `${name} ${lang} model fields types definition`;
        case 'service':     return `${name} class implementation`;
        case 'function':    return `${name} function implementation`;
        case 'method_call': return `${name} method implementation`;
        case 'constant':    return `${name} constant definition`;
        default:            return `${name} definition`;
    }
}

// ---------------------------------------------------------------------------
// _resolveAllDependencies — 3-round parallel resolution
// ---------------------------------------------------------------------------

/**
 * Resolve all dependencies using three rounds of parallel resolution:
 *
 *   **Round 1** — Strategy-based (all in parallel):
 *     - `read_file`:       Read the known file; for method_call deps extract
 *                          the specific method body.
 *     - `symbol_lookup`:   DB lookup → read the file range (full class body).
 *     - `semantic_search`: Targeted per-dep query against the embedding index.
 *
 *   **Round 2** — Gap detection (`_detectGapsAndResolve`):
 *     - Unresolved deps get targeted semantic follow-up queries.
 *     - Method calls where the class was found but the method wasn't in range
 *       get `_extractMethodBody` applied to the class content.
 *
 *   **Round 3** — Emergency fallback:
 *     - Only if > 50% unresolved AND the DB is empty.
 *     - Single broad semantic query with the raw code (preserves old behavior).
 */
async function _resolveAllDependencies(
    nodes:            DependencyNode[],
    db:               ConductorDb | null,
    workspaceFolders: vscodeT.WorkspaceFolder[],
    vscode:           typeof vscodeT,
): Promise<Map<string, ResolvedDependency>> {
    const resolved = new Map<string, ResolvedDependency>();
    if (nodes.length === 0) return resolved;

    // --- Round 1: strategy-based parallel resolution --------------------------
    const round1Tasks = nodes.map(async (node): Promise<void> => {
        try {
            if (node.strategy === 'read_file' && node.knownPath) {
                // Read the known file via VS Code API.
                const content = await _readWorkspaceFile(node.knownPath, workspaceFolders, vscode);
                if (!content) return;

                // For method_call deps, extract the specific method body.
                if (node.kind === 'method_call') {
                    const methodBody = _extractMethodBody(content, node.name, node.knownPath);
                    if (methodBody) {
                        resolved.set(node.name, {
                            dep: node, path: node.knownPath,
                            content: methodBody.content, resolvedVia: 'file_read',
                        });
                        return;
                    }
                }
                // For other deps (or if method extraction failed), include the full file.
                // Cap at 8 KB to avoid blowing up the context.
                const capped = content.length > 8_000
                    ? content.slice(0, content.lastIndexOf('\n', 8_000)) + '\n… [truncated]'
                    : content;
                resolved.set(node.name, {
                    dep: node, path: node.knownPath, content: capped, resolvedVia: 'file_read',
                });

            } else if (node.strategy === 'symbol_lookup' && db) {
                // DB symbol lookup — read the file range from the symbol table.
                const sym = node.knownPath
                    ? db.getSymbolByPathAndName(node.knownPath, node.name)
                    : (db.getSymbolsByName(node.name)[0] ?? null);
                if (!sym) return;

                const content = await _readWorkspaceFile(sym.path, workspaceFolders, vscode);
                if (!content) return;

                const lines = content.split('\n');
                const startLine = Math.max(0, sym.start_line);
                const endLine   = Math.min(lines.length, sym.end_line + 1);
                let slice = lines.slice(startLine, endLine).join('\n');

                // For method_call deps on a class, extract the specific method.
                if (node.kind === 'method_call' && node.receiver) {
                    const methodBody = _extractMethodBody(content, node.name, sym.path);
                    if (methodBody) {
                        slice = methodBody.content;
                    }
                }

                if (slice.trim()) {
                    resolved.set(node.name, {
                        dep: node, path: sym.path, content: slice, resolvedVia: 'symbol_db',
                    });
                }

            }
        } catch { /* non-fatal — individual dep failure */ }
    });

    await Promise.all(round1Tasks);

    // --- Round 2: gap detection & targeted follow-ups -------------------------
    await _detectGapsAndResolve(nodes, resolved);

    return resolved;
}

// ---------------------------------------------------------------------------
// _detectGapsAndResolve — Round 2 follow-up queries
// ---------------------------------------------------------------------------

/**
 * Detect unresolved deps after Round 1 and attempt method body extraction.
 *
 *   - Method calls where the receiver class was resolved but the specific
 *     method wasn't found → apply `_extractMethodBody` on the class content.
 */
async function _detectGapsAndResolve(
    nodes:    DependencyNode[],
    resolved: Map<string, ResolvedDependency>,
): Promise<void> {
    const unresolved = nodes.filter(n => !resolved.has(n.name));
    if (unresolved.length === 0) return;

    console.log(`${LOG} [2.7] Round 2 gap detection: ${unresolved.map(n => n.name).join(', ')}`);

    const round2Tasks = unresolved.map(async (node): Promise<void> => {
        try {
            // For method_call deps: if the receiver class was resolved, try to
            // extract the method body from the class content.
            if (node.kind === 'method_call' && node.receiver) {
                const receiverResult = resolved.get(node.receiver);
                if (receiverResult && receiverResult.content) {
                    const methodBody = _extractMethodBody(
                        receiverResult.content, node.name, receiverResult.path,
                    );
                    if (methodBody) {
                        resolved.set(node.name, {
                            dep: node, path: receiverResult.path,
                            content: methodBody.content, resolvedVia: 'symbol_db',
                        });
                    }
                }
            }
        } catch { /* non-fatal */ }
    });

    await Promise.all(round2Tasks);
}

// ---------------------------------------------------------------------------
// _extractMethodBody — locate a specific method within file content
// ---------------------------------------------------------------------------

/**
 * Locate a specific method/function within file content and return its body.
 *
 * - **Python**: Find `def method_name(` or `async def method_name(`, read
 *   until indentation returns to the same or lower level.
 * - **TypeScript/JS**: Find `method_name(`, count braces to find the closing `}`.
 * - Returns the full method body including signature, or null.
 */
function _extractMethodBody(
    fileContent: string,
    methodName:  string,
    filePath:    string,
): { content: string; startLine: number; endLine: number } | null {
    const lines = fileContent.split('\n');
    const ext = path.extname(filePath).toLowerCase();
    const isPython = ext === '.py';

    for (let i = 0; i < lines.length; i++) {
        const line = lines[i];

        if (isPython) {
            // Match `def method_name(` or `async def method_name(`
            const pyRe = new RegExp(`^(\\s*)(?:async\\s+)?def\\s+${_escapeRegex(methodName)}\\s*\\(`);
            const pyMatch = pyRe.exec(line);
            if (!pyMatch) continue;

            const baseIndent = pyMatch[1].length;
            let endLine = i;
            for (let j = i + 1; j < lines.length; j++) {
                const nextLine = lines[j];
                if (!nextLine.trim()) { endLine = j; continue; }
                const nextIndent = nextLine.match(/^(\s*)/)?.[1].length ?? 0;
                if (nextIndent <= baseIndent) break;
                endLine = j;
            }
            return {
                content:   lines.slice(i, endLine + 1).join('\n'),
                startLine: i,
                endLine,
            };
        } else {
            // TypeScript / JavaScript: match `methodName(` with optional
            // keywords (async, public, private, etc.) before it.
            const tsRe = new RegExp(`(?:^|\\s)(?:async\\s+)?(?:public|private|protected|static|\\s)*${_escapeRegex(methodName)}\\s*[(<]`);
            if (!tsRe.test(line)) continue;

            // Count braces to find the end of the method body.
            let braceCount = 0;
            let foundOpen = false;
            let endLine = i;
            for (let j = i; j < lines.length; j++) {
                for (const ch of lines[j]) {
                    if (ch === '{') { braceCount++; foundOpen = true; }
                    if (ch === '}') braceCount--;
                }
                endLine = j;
                if (foundOpen && braceCount <= 0) break;
            }
            return {
                content:   lines.slice(i, endLine + 1).join('\n'),
                startLine: i,
                endLine,
            };
        }
    }

    return null;
}

/** Escape special regex characters in a string. */
function _escapeRegex(s: string): string {
    return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

// ---------------------------------------------------------------------------
// _readWorkspaceFile — helper to read a file from the workspace
// ---------------------------------------------------------------------------

/**
 * Read a workspace-relative file's full content via the VS Code API.
 * Returns null if the file is not found in any workspace folder.
 */
async function _readWorkspaceFile(
    relativePath:     string,
    workspaceFolders: vscodeT.WorkspaceFolder[],
    vscode:           typeof vscodeT,
): Promise<string | null> {
    for (const folder of workspaceFolders) {
        const candidate = vscode.Uri.joinPath(folder.uri, relativePath);
        try {
            await vscode.workspace.fs.stat(candidate);
            const doc = await vscode.workspace.openTextDocument(candidate);
            return doc.getText();
        } catch { /* try next folder */ }
    }
    return null;
}

function _resolveImportPaths(
    imports:     string[],
    currentFile: string,
    language?:   string,
): string[] {
    const currentDir = path.dirname(currentFile);
    const seen       = new Set<string>();
    const results: string[] = [];

    const add = (p: string) => {
        const norm = p.replace(/\\/g, '/');
        if (!seen.has(norm)) { seen.add(norm); results.push(norm); }
    };

    const FROM_RE    = /from\s+['"]([^'"]+)['"]/;
    const REQUIRE_RE = /require\s*\(\s*['"]([^'"]+)['"]/;
    const PY_FROM_RE = /^from\s+([\w.]+)\s+import/;
    const PY_IMP_RE  = /^import\s+([\w.]+)/;

    for (const imp of imports) {
        // ---- TypeScript / JavaScript (relative or require) -------------------
        const tsPath = FROM_RE.exec(imp)?.[1] ?? REQUIRE_RE.exec(imp)?.[1];
        if (tsPath) {
            if (tsPath.startsWith('.')) {
                let resolved = path.join(currentDir, tsPath);
                if (!path.extname(resolved)) resolved += '.ts';
                add(resolved);
            }
            continue;
        }

        // ---- Python ----------------------------------------------------------
        if (language === 'python' || imp.startsWith('from ') || imp.startsWith('import ')) {
            // Relative: `from .models import X` or `from ..utils import Y`
            const relMatch = /^from\s+(\.+)([\w.]*)/.exec(imp);
            if (relMatch) {
                const dots   = relMatch[1].length;        // number of dots
                const module = relMatch[2];               // may be empty
                let base = currentDir;
                for (let i = 1; i < dots; i++) base = path.dirname(base);
                if (module) {
                    const modPath = module.replace(/\./g, '/');
                    add(path.join(base, modPath + '.py'));
                    add(path.join(base, modPath + '/__init__.py'));
                }
                continue;
            }

            // Absolute: `from foo.bar.baz import X` or `import foo.bar`
            const pyFrom = PY_FROM_RE.exec(imp)?.[1] ?? PY_IMP_RE.exec(imp)?.[1];
            if (pyFrom && !pyFrom.startsWith('.')) {
                const modPath = pyFrom.replace(/\./g, '/');
                // Only the leaf module is interesting; skip stdlib / site-packages
                // by limiting depth (top-level single-component modules are likely stdlib).
                if (modPath.includes('/')) {
                    add(modPath + '.py');
                    add(modPath + '/__init__.py');
                }
            }
        }
    }

    return results;
}

/**
 * Read file slices defined by the context plan using the VS Code workspace API.
 * Returns a Map from workspace-relative path to sliced content.
 * Individual file read errors are caught and logged, not propagated.
 */
async function _executePlan(
    plan:             ReadFileOp[],
    workspaceFolders: vscodeT.WorkspaceFolder[],
    vscode:           typeof vscodeT,
): Promise<Map<string, string>> {
    const contents = new Map<string, string>();

    for (const op of plan) {
        try {
            // Resolve the workspace-relative path to an absolute URI.
            let fileUri: vscodeT.Uri | undefined;
            for (const folder of workspaceFolders) {
                const candidate = vscode.Uri.joinPath(folder.uri, op.path);
                try {
                    await vscode.workspace.fs.stat(candidate);
                    fileUri = candidate;
                    break;
                } catch { /* not in this folder */ }
            }
            if (!fileUri) {
                console.log(`${LOG} [5/8] File not found in workspace: ${op.path}`);
                continue;
            }

            const doc = await vscode.workspace.openTextDocument(fileUri);
            const allLines = doc.getText().split('\n');

            const start = op.start_line ?? 0;
            const end   = op.end_line   ?? allLines.length;
            let text = allLines.slice(start, end).join('\n');

            // Apply byte cap.
            if (text.length > op.max_bytes) {
                text = text.slice(0, op.max_bytes) + '\n… [truncated]';
            }

            contents.set(op.path, text);
        } catch (err) {
            console.log(`${LOG} [5/8] Could not read ${op.path} (skipped):`, err);
        }
    }

    return contents;
}

/**
 * Construct the AssemblerInput from the pipeline inputs and the read file
 * contents.  The current-file snippet uses the originally selected code so
 * the LLM sees the exact text the user had selected, not a re-read slice.
 */
function _buildXmlInput(
    input:                  PipelineInput,
    lspResult:              { definition?: { path: string } },
    fileContents:           Map<string, string>,
    question:               string,
    fullFileContent:        string | undefined,
    typeDefinitionSnippets: Array<{ path: string; content: string }>,
    projectMetadata:        ProjectMetadata | null,
): Parameters<typeof assembleXmlPrompt>[0] {
    // Current file — always the selected code text.
    const currentFile: FileSnippet = {
        path:    input.relativePath,
        content: input.code,
        role:    'current',
    };

    // Full current-file content — always included so the LLM sees imports,
    // surrounding functions, and class definitions even when LSP/semantic fail.
    // To avoid byte-for-byte repetition, annotate the selected lines range so
    // the model knows which part is the "focus" without re-reading it fully.
    let fullFileSnippet: FileSnippet | undefined;
    if (fullFileContent && fullFileContent.trim() !== input.code.trim()) {
        // Insert a marker at the selection start line so the LLM can locate it.
        const lines = fullFileContent.split('\n');
        const sel0  = Math.max(0, input.startLine - 1);   // 0-based
        const sel1  = Math.min(lines.length, input.endLine); // exclusive
        lines.splice(sel0, 0, `# ↓ selected lines ${input.startLine}–${input.endLine}`);
        lines.splice(sel1 + 1, 0, `# ↑ end of selection`);
        fullFileSnippet = {
            path:    input.relativePath,
            content: lines.join('\n'),
            role:    'related',
        };
    }

    // Definition file (if any).
    let definition: FileSnippet | undefined;
    if (lspResult.definition) {
        const defContent = fileContents.get(lspResult.definition.path);
        if (defContent !== undefined) {
            definition = {
                path:    lspResult.definition.path,
                content: defContent,
                role:    'definition',
            };
        }
    }

    // All other files from the plan (excluding the definition file and the
    // current file since we already include it as fullFileSnippet).
    const relatedFiles: FileSnippet[] = [];
    if (fullFileSnippet) relatedFiles.push(fullFileSnippet);
    for (const [filePath, content] of fileContents) {
        if (filePath === input.relativePath) continue;
        if (lspResult.definition && filePath === lspResult.definition.path) continue;
        relatedFiles.push({ path: filePath, content, role: 'related' });
    }

    // Type definitions from Stage 2.8 — deduplicated against files already included.
    const includedPaths = new Set<string>([
        input.relativePath,
        ...(lspResult.definition ? [lspResult.definition.path] : []),
        ...Array.from(fileContents.keys()),
    ]);
    for (const { path: p, content } of typeDefinitionSnippets) {
        if (!includedPaths.has(p)) {
            relatedFiles.push({ path: p, content, role: 'related' });
            includedPaths.add(p);
        }
    }

    // Convert ProjectMetadata → ProjectMetadataInput (drop null).
    const metaInput: ProjectMetadataInput | undefined = projectMetadata
        ? {
            name:       projectMetadata.name,
            languages:  projectMetadata.languages,
            frameworks: projectMetadata.frameworks,
            structure:  projectMetadata.structure,
        }
        : undefined;

    return { currentFile, definition, relatedFiles, question, projectMetadata: metaInput };
}

/**
 * POST the code snippet to the backend `/api/context/explain-rich` endpoint.
 *
 * The backend runs an agentic loop (AgentLoopService) that iteratively calls
 * code-intelligence tools — read_file, find_symbol, find_references, grep,
 * get_callers, etc. — to gather context before producing an explanation.
 *
 * This replaces the old approach of assembling a large XML prompt in the
 * extension (stages 1–6) and forwarding it to the LLM directly. The agent
 * explores the codebase server-side, yielding richer and more accurate results.
 *
 * Note: the `_xmlPrompt` parameter is kept in the signature so callers do not
 * need to change — it is no longer sent to the backend.
 */
/** Human-readable description for a tool call. */
function _toolLabel(tool: string, params: Record<string, any>): string {
    switch (tool) {
        case 'read_file':        return `Reading ${params.file_path || params.path || 'file'}`;
        case 'grep':             return `Searching for "${params.pattern || '...'}"`;
        case 'find_symbol':      return `Finding symbol "${params.name || '...'}"`;
        case 'find_references':  return `Finding references to "${params.name || params.symbol_name || '...'}"`;
        case 'file_outline':     return `Outlining ${params.file_path || params.path || 'file'}`;
        case 'list_files':       return `Listing files`;
        case 'get_dependencies': return `Checking dependencies`;
        case 'get_dependents':   return `Checking dependents`;
        case 'git_log':          return `Checking git history`;
        case 'git_diff':         return `Comparing changes`;
        case 'git_blame':        return `Tracing authorship of ${params.file || 'file'}`;
        case 'git_show':         return `Reading commit ${params.commit || '...'}`;
        case 'find_tests':       return `Finding tests for "${params.name || '...'}"`;
        case 'test_outline':     return `Analyzing test structure of ${params.path || 'file'}`;
        case 'ast_search':       return `AST pattern search`;
        case 'get_callers':      return `Finding callers of "${params.function_name || '...'}"`;
        case 'get_callees':      return `Finding callees of "${params.function_name || '...'}"`;
        case 'trace_variable':   return `Tracing data flow of "${params.variable_name || '...'}" ${params.direction || 'forward'}`;
        default:                 return `Running ${tool}`;
    }
}

export interface ThinkingStep {
    kind: string;
    iteration?: number;
    text?: string;
    tool?: string;
    params?: Record<string, any>;
    summary?: string;
    success?: boolean;
}

async function _callLlm(
    _xmlPrompt: string,   // retained for call-site compatibility; not sent to backend
    input:      PipelineInput,
): Promise<{ explanation: string; model: string; structured?: Record<string, string>; thinking_steps: ThinkingStep[] }> {
    const progress = input.onProgress;
    const requestBody = JSON.stringify({
        room_id:    input.workspaceId ?? '',
        code:       input.code,
        file_path:  input.relativePath,
        language:   input.language,
        start_line: input.startLine,
        end_line:   input.endLine,
        question:   input.question ?? null,
    });

    // --- Try SSE streaming endpoint first ---
    const streamUrl = `${input.backendUrl}/api/context/explain-rich/stream`;
    console.log(`${LOG} [LLM] POST ${streamUrl} — SSE agentic explain (room=${input.workspaceId ?? 'none'})`);

    try {
        const response = await fetch(streamUrl, {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    requestBody,
        });

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }

        // Parse SSE events from the response body stream
        const reader = response.body!.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let finalAnswer = '';
        let finalModel  = 'ai';
        const collectedSteps: ThinkingStep[] = [];

        // eslint-disable-next-line no-constant-condition
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });

            // Split on double newline (SSE event boundary)
            const parts = buffer.split('\n\n');
            buffer = parts.pop()!;     // keep the incomplete tail

            for (const part of parts) {
                const lines = part.split('\n');
                let eventKind = '';
                let eventData = '';
                for (const line of lines) {
                    if (line.startsWith('event: '))     eventKind = line.slice(7);
                    else if (line.startsWith('data: ')) eventData = line.slice(6);
                }
                if (!eventKind || !eventData) continue;

                let data: Record<string, any>;
                try { data = JSON.parse(eventData); } catch { continue; }

                // Emit progress to the UI + collect thinking steps
                if (eventKind === 'thinking') {
                    const text = (data.text as string || '').slice(0, 120);
                    progress?.({ phase: 'agent', kind: 'thinking', message: text || 'Thinking...', detail: data });
                    collectedSteps.push({ kind: 'thinking', iteration: data.iteration, text: data.text });
                } else if (eventKind === 'tool_call') {
                    const label = _toolLabel(data.tool, data.params || {});
                    progress?.({
                        phase: 'agent', kind: 'tool_call',
                        message: label,
                        detail: { tool: data.tool, iteration: data.iteration },
                    });
                    collectedSteps.push({ kind: 'tool_call', iteration: data.iteration, tool: data.tool, params: data.params });
                } else if (eventKind === 'tool_result') {
                    progress?.({
                        phase: 'agent', kind: 'tool_result',
                        message: `${data.tool}: ${data.summary || 'done'}`,
                        detail: { tool: data.tool, success: data.success, iteration: data.iteration },
                    });
                    collectedSteps.push({ kind: 'tool_result', iteration: data.iteration, tool: data.tool, summary: data.summary, success: data.success });
                } else if (eventKind === 'done') {
                    finalAnswer = data.answer || '';
                    finalModel  = data.model  || 'ai';
                    // Backend also sends thinking_steps in done event — prefer those
                    if (data.thinking_steps?.length) {
                        collectedSteps.length = 0;
                        for (const s of data.thinking_steps) { collectedSteps.push(s); }
                    }
                } else if (eventKind === 'error') {
                    finalAnswer = data.answer || '';
                    finalModel  = data.model  || 'ai';
                    if (data.error) {
                        console.error(`${LOG} [LLM] Agent error: ${data.error}`);
                    }
                }
            }
        }

        console.log(`${LOG} [LLM] SSE stream complete — answer=${finalAnswer.length} chars, model=${finalModel}, steps=${collectedSteps.length}`);
        return { explanation: finalAnswer, model: finalModel, thinking_steps: collectedSteps };

    } catch (streamErr) {
        // Fall back to non-streaming endpoint
        console.log(`${LOG} [LLM] SSE stream unavailable, falling back to non-streaming:`, streamErr);
        progress?.({ phase: 'agent', message: 'Waiting for AI response...' });

        const url = `${input.backendUrl}/api/context/explain-rich`;
        const response = await fetch(url, {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    requestBody,
        });

        if (!response.ok) {
            const body = await response.text().catch(() => '');
            throw new Error(`/api/context/explain-rich returned HTTP ${response.status}: ${body}`);
        }

        const data = (await response.json()) as {
            explanation: string;
            model: string;
            structured?: Record<string, string> | null;
            thinking_steps?: ThinkingStep[];
        };

        return {
            explanation:    data.explanation,
            model:          data.model,
            structured:     data.structured ?? undefined,
            thinking_steps: data.thinking_steps || [],
        };
    }
}
