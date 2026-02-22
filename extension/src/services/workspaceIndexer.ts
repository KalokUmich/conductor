/**
 * Two-phase workspace indexer for Conductor.
 *
 * Phase 1 (blocking, target <5s):
 *   Reuse `scanWorkspaceV1()` to collect and upsert file metadata (path,
 *   mtime, sha1) into the Conductor SQLite database.
 *
 * Phase 2 (fire-and-forget, non-blocking):
 *   For every file that needs re-indexing (new or mtime-changed), extract
 *   symbols via `extractSymbols()` and enqueue them for cloud embedding via
 *   `EmbeddingQueue`.
 *
 * The caller receives an `IndexProgress` snapshot after Phase 1 completes.
 * Phase 2 reports progress via the optional `onProgress` callback.
 *
 * No VS Code dependency — fully testable under the Node.js test runner.
 *
 * @module services/workspaceIndexer
 */

import * as path from 'path';
import * as crypto from 'crypto';
import * as fs from 'fs/promises';

import { ConductorDb, SymbolRow } from './conductorDb';
import { scanWorkspaceV1, LANG_MAP } from './workspaceScanner';
import { extractSymbols, FileSymbol } from './symbolExtractor';
import { EmbeddingQueue, EmbeddingJobItem } from './embeddingQueue';
import { EmbeddingClient } from './embeddingClient';

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

export interface IndexProgress {
    phase: 'scanning' | 'extracting' | 'embedding' | 'done';
    filesScanned: number;
    totalFiles: number;
    /** Files processed so far in Phase 2 (symbol-extraction loop). */
    filesIndexed: number;
    /** Files that actually need re-indexing (set at Phase 2 start). */
    staleFilesCount: number;
    symbolsExtracted: number;
    embeddingsEnqueued: number;
    /** True when embedding is active; false in AST-only mode. */
    embeddingEnabled: boolean;
    /** True when this is a small incremental update (file watcher / fresh check). */
    isIncremental: boolean;
}

export type ProgressCallback = (progress: IndexProgress) => void;

export interface IndexOptions {
    /**
     * Embedding model ID.  When omitted, Phase 2 skips embedding and only
     * extracts AST symbols — LSP/search features still work.
     */
    embeddingModel?: string;
    /** Required when `embeddingModel` is set. */
    embeddingDim?: number;
    backendUrl: string;
    /** Maximum milliseconds to wait for Phase 1 (default 5000). */
    phase1TimeoutMs?: number;
    onProgress?: ProgressCallback;
    /** Called when an embedding batch fails after retry. */
    onEmbeddingError?: (err: Error) => void;
    /**
     * Absolute paths of files that should be processed first in Phase 2
     * (e.g. currently open editor tabs).
     */
    priorityFiles?: string[];
    /**
     * Called immediately after the EmbeddingQueue is created in Phase 2.
     * The caller can store this reference to cancel the queue later.
     */
    onQueueReady?: (queue: EmbeddingQueue) => void;
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const LOG = '[WorkspaceIndexer]';

/** Number of files to process between event-loop yields in Phase 2. */
const PHASE2_BATCH_SIZE = 50;

/**
 * Max items per embedding job sent to the queue.
 * Cohere Embed on Bedrock accepts up to 96 texts per InvokeModel call.
 */
const EMBEDDING_BATCH_SIZE = 96;

/**
 * Monotonically increasing run ID.  Incrementing this cancels any Phase 2
 * that is already in progress — it will notice the stale ID and exit.
 */
let _currentRunId = 0;

/**
 * Cancel any Phase 2 that is currently running.
 * Safe to call when no index is active.
 */
export function cancelCurrentIndex(): void {
    _currentRunId++;
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Run the two-phase workspace indexing pipeline.
 *
 * Phase 1 blocks for up to `phase1TimeoutMs` (default 5s).
 * Phase 2 runs in the background after Phase 1 resolves.
 *
 * @returns The progress snapshot at the end of Phase 1.
 */
export async function indexWorkspace(
    workspaceRoot: string,
    db: ConductorDb,
    options: IndexOptions,
): Promise<IndexProgress> {
    const runId    = ++_currentRunId;   // claim a new run ID; any prior Phase 2 will abort
    const timeoutMs = options.phase1TimeoutMs ?? 5000;

    const embeddingEnabled = !!(options.embeddingModel && options.embeddingDim);

    const progress: IndexProgress = {
        phase: 'scanning',
        filesScanned: 0,
        totalFiles: 0,
        filesIndexed: 0,
        staleFilesCount: 0,
        symbolsExtracted: 0,
        embeddingsEnqueued: 0,
        embeddingEnabled,
        isIncremental: false,
    };
    options.onProgress?.(progress);

    // -----------------------------------------------------------------
    // Phase 1 — fast metadata scan (blocking, with timeout)
    // -----------------------------------------------------------------
    console.log(`${LOG} Phase 1: scanning workspace (timeout=${timeoutMs}ms)`);
    const phase1Start = performance.now();

    try {
        await Promise.race([
            scanWorkspaceV1(workspaceRoot, db),
            _timeout(timeoutMs),
        ]);
    } catch (err) {
        // Timeout or scan error — Phase 1 produces partial results, Phase 2
        // still runs on whatever was upserted.
        console.warn(`${LOG} Phase 1 interrupted: ${err}`);
    }

    const allFiles = db.getAllFiles();
    progress.filesScanned = allFiles.length;
    progress.totalFiles = allFiles.length;
    progress.phase = 'extracting';
    options.onProgress?.(progress);

    const phase1Ms = performance.now() - phase1Start;
    console.log(`${LOG} Phase 1 complete: ${allFiles.length} files in ${phase1Ms.toFixed(0)}ms`);

    // -----------------------------------------------------------------
    // Phase 2 — background symbol extraction + embedding (fire-and-forget)
    // -----------------------------------------------------------------
    _runPhase2(workspaceRoot, db, options, progress, runId).catch(err => {
        console.error(`${LOG} Phase 2 error:`, err);
        // Always send 'done' so the WebView overlay is dismissed even on crash.
        progress.phase = 'done';
        options.onProgress?.(progress);
    });

    return { ...progress };
}

// ---------------------------------------------------------------------------
// Phase 2 implementation
// ---------------------------------------------------------------------------

async function _runPhase2(
    workspaceRoot: string,
    db: ConductorDb,
    options: IndexOptions,
    progress: IndexProgress,
    runId: number,
): Promise<void> {
    const phase2Start = performance.now();
    const staleFiles = db.getFilesNeedingReindex();
    progress.staleFilesCount = staleFiles.length;
    console.log(`${LOG} Phase 2: ${staleFiles.length} file(s) need re-indexing`);

    if (staleFiles.length === 0) {
        progress.phase = 'done';
        db.setMeta('last_scan_at', Date.now().toString());
        options.onProgress?.(progress);
        return;
    }

    const embeddingEnabled = !!(options.embeddingModel && options.embeddingDim);
    const client = embeddingEnabled ? new EmbeddingClient(options.backendUrl) : null;
    const queue  = embeddingEnabled && client ? new EmbeddingQueue(client, db) : null;

    // Expose the queue so the caller can cancel it if needed.
    if (queue) options.onQueueReady?.(queue);

    // Sort stale files so priority files (open editors) are processed first.
    const prioritySet = new Set(
        (options.priorityFiles ?? []).map(p => path.relative(workspaceRoot, p)),
    );
    const sortedStale = [
        ...staleFiles.filter(f => prioritySet.has(f.path)),
        ...staleFiles.filter(f => !prioritySet.has(f.path)),
    ];

    let filesProcessed = 0;
    let pendingEmbeddingItems: EmbeddingJobItem[] = [];

    for (const file of sortedStale) {
        // Stop if a newer index run has been requested.
        if (runId !== _currentRunId) {
            console.log(`${LOG} Phase 2 cancelled (runId=${runId}, current=${_currentRunId})`);
            queue?.cancel();
            break;
        }

        try {
            const absPath = path.join(workspaceRoot, file.path);

            // --- Extract symbols ---
            const extracted = extractSymbols(absPath);
            const symbolRows: SymbolRow[] = extracted.symbols.map((sym: FileSymbol) => ({
                id:         `${file.path}::${sym.name}`,
                path:       file.path,
                name:       sym.name,
                kind:       sym.kind,
                start_line: sym.range.start.line,
                end_line:   sym.range.end.line,
                signature:  sym.signature,
            }));

            db.replaceSymbolsForFile(file.path, symbolRows);
            progress.symbolsExtracted += symbolRows.length;

            // --- Prepare embedding items (flush within symbol loop to honour batch cap) ---
            for (const row of symbolRows) {
                const textToEmbed = row.signature || row.name;
                const sha1 = crypto.createHash('sha1').update(textToEmbed).digest('hex');
                pendingEmbeddingItems.push({
                    symbolId: row.id,
                    text: textToEmbed,
                    sha1,
                });

                if (queue && pendingEmbeddingItems.length >= EMBEDDING_BATCH_SIZE) {
                    queue.enqueue({
                        items: pendingEmbeddingItems,
                        model: options.embeddingModel!,
                        dim: options.embeddingDim!,
                        onError: (err) => options.onEmbeddingError?.(err),
                    });
                    progress.embeddingsEnqueued += pendingEmbeddingItems.length;
                    pendingEmbeddingItems = [];
                }
            }

            // Mark file as indexed.
            db.upsertFiles([{
                ...file,
                last_indexed_at: Date.now(),
            }]);
        } catch (fileErr) {
            // Isolate per-file errors so one bad file can't abort the whole phase.
            console.warn(`${LOG} Phase 2 skipping file ${file.path}:`, fileErr);
        }

        filesProcessed++;
        progress.filesIndexed = filesProcessed;

        // Yield to the event loop periodically to stay responsive.
        if (filesProcessed % PHASE2_BATCH_SIZE === 0) {
            progress.phase = 'extracting';
            options.onProgress?.(progress);
            await new Promise(r => setTimeout(r, 0));
        }
    }

    // Flush remaining embedding items.
    if (queue && pendingEmbeddingItems.length > 0) {
        queue.enqueue({
            items: pendingEmbeddingItems,
            model: options.embeddingModel!,
            dim: options.embeddingDim!,
            onError: (err) => options.onEmbeddingError?.(err),
        });
        progress.embeddingsEnqueued += pendingEmbeddingItems.length;
    }

    progress.phase = 'done';
    db.setMeta('last_scan_at', Date.now().toString());
    options.onProgress?.(progress);

    const phase2Ms = performance.now() - phase2Start;
    console.log(
        `${LOG} Phase 2 complete: ${filesProcessed} files, ` +
        `${progress.symbolsExtracted} symbols, ` +
        `${progress.embeddingsEnqueued} embeddings enqueued ` +
        `in ${phase2Ms.toFixed(0)}ms`,
    );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function _timeout(ms: number): Promise<never> {
    return new Promise((_, reject) =>
        setTimeout(() => reject(new Error(`Phase 1 timed out after ${ms}ms`)), ms),
    );
}

// ---------------------------------------------------------------------------
// Incremental single-file reindex (used by the file-system watcher)
// ---------------------------------------------------------------------------

/**
 * Re-extract symbols from one file and enqueue its embeddings.
 *
 * Designed for the VS Code `FileSystemWatcher` hot-update path.
 * Uses the same symbol extraction + embedding pipeline as Phase 2 but
 * operates on a single file so it completes in milliseconds.
 *
 * @returns Number of symbols found in the file.
 */
export async function reindexSingleFile(
    workspaceRoot: string,
    absPath: string,
    db: ConductorDb,
    options: Pick<IndexOptions, 'embeddingModel' | 'embeddingDim' | 'backendUrl' | 'onEmbeddingError'>,
): Promise<number> {
    const relPath = path.relative(workspaceRoot, absPath);

    try {
        const stat = await fs.stat(absPath);
        const content = await fs.readFile(absPath);
        const sha1 = crypto.createHash('sha1').update(content).digest('hex');
        const lang = LANG_MAP[path.extname(absPath).toLowerCase()] ?? '';

        // Upsert file metadata
        db.upsertFiles([{ path: relPath, mtime: stat.mtimeMs, size: stat.size, lang, sha1, last_indexed_at: null }]);

        // Extract symbols
        const extracted = extractSymbols(absPath);
        const symbolRows: SymbolRow[] = extracted.symbols.map((sym: FileSymbol) => ({
            id:         `${relPath}::${sym.name}`,
            path:       relPath,
            name:       sym.name,
            kind:       sym.kind,
            start_line: sym.range.start.line,
            end_line:   sym.range.end.line,
            signature:  sym.signature,
        }));
        db.replaceSymbolsForFile(relPath, symbolRows);

        // Embed if configured
        if (options.embeddingModel && options.embeddingDim && symbolRows.length > 0) {
            const client = new EmbeddingClient(options.backendUrl);
            const queue  = new EmbeddingQueue(client, db);
            const items: EmbeddingJobItem[] = symbolRows.map(row => ({
                symbolId: row.id,
                text:     row.signature || row.name,
                sha1:     crypto.createHash('sha1').update(row.signature || row.name).digest('hex'),
            }));
            for (let i = 0; i < items.length; i += EMBEDDING_BATCH_SIZE) {
                queue.enqueue({
                    items: items.slice(i, i + EMBEDDING_BATCH_SIZE),
                    model: options.embeddingModel,
                    dim:   options.embeddingDim,
                    onError: (err) => options.onEmbeddingError?.(err),
                });
            }
        }

        // Mark as indexed
        db.upsertFiles([{ path: relPath, mtime: stat.mtimeMs, size: stat.size, lang, sha1, last_indexed_at: Date.now() }]);

        return symbolRows.length;
    } catch (err) {
        console.warn(`${LOG} reindexSingleFile failed for ${relPath}:`, err);
        return 0;
    }
}
