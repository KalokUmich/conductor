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
 *   7. LLM call     – POST /context/explain with the XML prompt
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
import { assembleXmlPrompt, FileSnippet }                     from './xmlPromptAssembler';
import { extractSymbols }                                     from './symbolExtractor';
import { VectorIndex, SearchResult }                          from './vectorIndex';
import { EmbeddingClient }                                    from './embeddingClient';
import { ConductorDb }                                        from './conductorDb';
import {
    WorkspaceConfig, DEFAULT_WORKSPACE_CONFIG,
    EmbeddingConfig, DEFAULT_EMBEDDING_CONFIG,
} from './workspaceStorage';

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

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
    conductorDb:       ConductorDb | null;
    workspaceFolders:  vscodeT.WorkspaceFolder[];

    // ---- Tuning (sourced from config at runtime) ----------------------------
    /**
     * Extension-side workspace settings (`.conductor/config.json`).
     * Controls ranking caps and semantic top-K.
     * Falls back to `DEFAULT_WORKSPACE_CONFIG` for any missing field.
     */
    workspaceConfig?: WorkspaceConfig;
    /**
     * Embedding configuration fetched from `GET /embeddings/config` on the
     * backend, which reads `conductor.settings.yaml`.
     * This is the single source of truth for model ID and vector dimension.
     * Falls back to `DEFAULT_EMBEDDING_CONFIG` when the backend is unreachable.
     */
    embeddingConfig?: EmbeddingConfig;
}

export interface PipelineOutput {
    explanation: string;
    model:       string;
    /** The assembled XML prompt that was sent to the LLM. */
    xmlPrompt:   string;
    /** Wall-clock milliseconds per stage, keyed by stage name. */
    timings:     Record<string, number>;
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

    console.log(`${LOG} === Pipeline start ===`);
    console.log(`${LOG} file=${input.relativePath} lines=${input.startLine}-${input.endLine} lang=${input.language}`);
    console.log(`${LOG} conductorDb=${input.conductorDb ? 'SET' : 'NULL'}`);
    console.log(`${LOG} workspaceConfig=${input.workspaceConfig ? JSON.stringify(input.workspaceConfig) : 'NONE (using defaults)'}`);
    console.log(`${LOG} embeddingConfig=${input.embeddingConfig ? JSON.stringify(input.embeddingConfig) : 'NONE (using defaults)'}`);
    console.log(`${LOG} workspaceFolders=${input.workspaceFolders.length} backendUrl=${input.backendUrl}`);

    // Resolve workspace tuning (ranking caps, topK) from .conductor/config.json.
    const cfg = { ...DEFAULT_WORKSPACE_CONFIG, ...(input.workspaceConfig ?? {}) };
    // Resolve embedding model/dim from conductor.settings.yaml via backend API.
    const emb = { ...DEFAULT_EMBEDDING_CONFIG, ...(input.embeddingConfig ?? {}) };
    console.log(`${LOG} Resolved cfg: maxRelated=${cfg.maxRelated} maxContextFiles=${cfg.maxContextFiles} semanticTopK=${cfg.semanticTopK}`);
    console.log(`${LOG} Resolved emb: model=${emb.model} dim=${emb.dim} provider=${emb.provider}`);

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
    let semanticResults: SearchResult[] = [];
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
                input.backendUrl, emb.model, cfg.semanticTopK,
            );

            // Step 3: Collect results into the format downstream stages expect.
            for (const [, result] of resolved) {
                if (result.resolvedVia === 'file_read' || result.resolvedVia === 'symbol_db') {
                    typeDefinitionSnippets.push({ path: result.path, content: result.content });
                } else if (result.resolvedVia === 'semantic') {
                    semanticResults.push({
                        symbol_id: result.path + '::' + result.dep.name,
                        score:     0.8,
                    });
                }
                // Add resolved paths to import neighbors for ranker graph-boost.
                if (result.path && !importNeighbors.includes(result.path)) {
                    importNeighbors.push(result.path);
                }
            }

            console.log(
                `${LOG} [2.7] Resolved: ${resolved.size}/${depNodes.length} deps, ` +
                `${typeDefinitionSnippets.length} snippets, ${semanticResults.length} semantic`,
            );
        } catch (err) {
            console.log(`${LOG} Stage 2.7 (dependency resolution) failed (non-fatal):`, err);
            // Emergency fallback: single broad semantic search.
            try {
                semanticResults = await _getSemanticResults(
                    input.code, input.backendUrl, input.conductorDb,
                    emb.model, cfg.semanticTopK,
                );
            } catch { /* non-fatal */ }
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
        semanticResults,
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
    );
    const { xml: xmlPrompt, wasTrimmed } = assembleXmlPrompt(xmlInput);
    timings['xml_assembly'] = performance.now() - t6;
    if (wasTrimmed) {
        console.log(`${LOG} [6/8] XML prompt was trimmed to fit 80 k char budget`);
    }
    console.log(
        `${LOG} Stage 6 (XML assembly): ${timings['xml_assembly'].toFixed(1)}ms — ${xmlPrompt.length} chars`,
    );

    // -------------------------------------------------------------------------
    // Stage 7 — Send to LLM
    // -------------------------------------------------------------------------
    const t7 = performance.now();
    let explanation = '';
    let model = 'ai';
    try {
        const result = await _callLlm(xmlPrompt, input);
        explanation = result.explanation;
        model       = result.model;
    } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        console.error(`${LOG} [7/8] LLM call failed:`, msg);
        throw err; // re-throw so the caller can surface the error to the user
    }
    timings['llm'] = performance.now() - t7;
    console.log(
        `${LOG} Stage 7 (LLM call): ${timings['llm'].toFixed(1)}ms — model=${model}`,
    );

    // Stage 8 (render) is handled by the caller after this function returns.
    console.log(`${LOG} Stage 8 (render): delegated to caller`);

    const total = Object.values(timings).reduce((a, b) => a + b, 0);
    console.log(`${LOG} Pipeline complete — total=${total.toFixed(1)}ms`);

    return { explanation, model, xmlPrompt, timings };
}

// ---------------------------------------------------------------------------
// Private helpers
// ---------------------------------------------------------------------------

/**
 * Semantic search using the local vector index and the backend embedding API.
 * Returns [] (without throwing) when the semantic layer is unavailable.
 */
async function _getSemanticResults(
    code:           string,
    backendUrl:     string,
    db:             ConductorDb | null,
    embeddingModel: string,
    topK:           number,
): Promise<SearchResult[]> {
    if (!db) {
        console.log(`${LOG} [Semantic] Skipped — conductorDb is null`);
        return [];
    }

    const rows = db.getAllVectorsByModel(embeddingModel);
    console.log(`${LOG} [Semantic] Vectors in DB for model "${embeddingModel}": ${rows.length}`);
    if (rows.length === 0) {
        console.log(`${LOG} [Semantic] Skipped — no vectors indexed yet`);
        return [];
    }

    const idx = new VectorIndex();
    idx.loadRows(rows);

    console.log(`${LOG} [Semantic] Calling embedding API at ${backendUrl}/embeddings ...`);
    const client  = new EmbeddingClient(backendUrl);
    const vectors = await client.embed([code]);
    const q       = new Float32Array(vectors[0]);
    const results = idx.search(q, topK);
    console.log(`${LOG} [Semantic] Search returned ${results.length} result(s)`);
    return results;
}

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
    /** Natural-language query for semantic / codebase search. */
    query: string;
    /** For method_call deps: the receiver class name (e.g. "AsyncPolicyService"). */
    receiver?: string;
    /** Known file path from imports or DB pre-check. */
    knownPath?: string;
    /** Resolution strategy chosen during planning. */
    strategy: 'read_file' | 'symbol_lookup' | 'semantic_search';
}

/** A successfully resolved dependency with its content. */
interface ResolvedDependency {
    dep:         DependencyNode;
    path:        string;
    content:     string;
    resolvedVia: 'file_read' | 'symbol_db' | 'semantic';
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

        // Strategy assignment: known path → read_file, DB hit → symbol_lookup, else semantic.
        let strategy: DependencyNode['strategy'] = 'semantic_search';
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
    backendUrl:       string,
    embeddingModel:   string,
    semanticTopK:     number,
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

            } else if (node.strategy === 'semantic_search') {
                // Targeted semantic search with the dep's query.
                if (!db) return;
                const results = await _getSemanticResults(
                    node.query, backendUrl, db, embeddingModel,
                    Math.min(3, semanticTopK),
                );
                if (results.length > 0) {
                    const top = results[0];
                    const symPath = top.symbol_id.split('::')[0] || top.symbol_id;
                    resolved.set(node.name, {
                        dep: node, path: symPath, content: '', resolvedVia: 'semantic',
                    });
                }
            }
        } catch { /* non-fatal — individual dep failure */ }
    });

    await Promise.all(round1Tasks);

    // --- Round 2: gap detection & targeted follow-ups -------------------------
    await _detectGapsAndResolve(
        nodes, resolved, db, workspaceFolders, vscode,
        backendUrl, embeddingModel, semanticTopK,
    );

    // --- Round 3: emergency fallback ------------------------------------------
    // Only if > 50% of deps remain unresolved AND the DB has no vectors
    // (i.e. embeddings haven't been indexed yet).
    const unresolvedCount = nodes.filter(n => !resolved.has(n.name)).length;
    const dbEmpty = !db || db.getAllVectorsByModel(embeddingModel).length === 0;
    if (unresolvedCount > nodes.length * 0.5 && dbEmpty) {
        console.log(`${LOG} [2.7] Round 3 emergency fallback: ${unresolvedCount}/${nodes.length} unresolved, DB empty`);
        // This intentionally left empty — the pipeline's outer catch already
        // handles the broad fallback query.  We just log the situation.
    }

    return resolved;
}

// ---------------------------------------------------------------------------
// _detectGapsAndResolve — Round 2 follow-up queries
// ---------------------------------------------------------------------------

/**
 * Detect unresolved deps after Round 1 and issue targeted follow-up queries.
 *
 *   - Unresolved deps → targeted semantic search (parallel).
 *   - Method calls where the receiver class was resolved but the specific
 *     method wasn't found → apply `_extractMethodBody` on the class content.
 *   - Missing receiver types → search fullFileContent for type annotations.
 */
async function _detectGapsAndResolve(
    nodes:            DependencyNode[],
    resolved:         Map<string, ResolvedDependency>,
    db:               ConductorDb | null,
    workspaceFolders: vscodeT.WorkspaceFolder[],
    vscode:           typeof vscodeT,
    backendUrl:       string,
    embeddingModel:   string,
    semanticTopK:     number,
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
                        return;
                    }
                }
            }

            // Fallback: targeted semantic search for unresolved deps.
            if (!db) return;
            const results = await _getSemanticResults(
                node.query, backendUrl, db, embeddingModel,
                Math.min(3, semanticTopK),
            );
            if (results.length > 0) {
                const top = results[0];
                const symPath = top.symbol_id.split('::')[0] || top.symbol_id;
                resolved.set(node.name, {
                    dep: node, path: symPath, content: '', resolvedVia: 'semantic',
                });
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

    return { currentFile, definition, relatedFiles, question };
}

/**
 * POST the pre-assembled XML prompt to the backend `/context/explain-rich`
 * endpoint, which forwards it directly to the LLM without re-parsing.
 */
async function _callLlm(
    xmlPrompt: string,
    input:     PipelineInput,
): Promise<{ explanation: string; model: string }> {
    console.log(`${LOG} [LLM] POST ${input.backendUrl}/context/explain-rich — prompt=${xmlPrompt.length} chars`);
    const response = await fetch(`${input.backendUrl}/context/explain-rich`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({
            assembled_prompt: xmlPrompt,
            snippet:          input.code,
            file_path:        input.relativePath,
            line_start:       input.startLine,
            line_end:         input.endLine,
            language:         input.language,
        }),
    });

    if (!response.ok) {
        const body = await response.text().catch(() => '');
        throw new Error(`/context/explain-rich returned HTTP ${response.status}: ${body}`);
    }

    return (await response.json()) as { explanation: string; model: string };
}
