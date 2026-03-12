/**
 * Two-phase workspace indexer for the Conductor context enricher.
 *
 * Phase 1 (synchronous-feeling, awaited by caller)
 * ------------------------------------------------
 * Scan the workspace with `scanWorkspaceV1()` to collect file metadata
 * into the ConductorDb.  A configurable timeout (`phase1TimeoutMs`) caps
 * the wall-clock time so the caller is never blocked indefinitely.
 *
 * Phase 2 (fire-and-forget, runs in the background)
 * --------------------------------------------------
 * For every file that needs re-indexing, extract symbols with
 * `extractSymbols()` and persist them to the DB.
 *
 * @module services/workspaceIndexer
 */

import * as path from 'path';
import * as crypto from 'crypto';

import { ConductorDb, SymbolRow } from './conductorDb';
import { scanWorkspaceV1 }        from './workspaceScanner';
import { extractSymbols }         from './symbolExtractor';

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

/** Phase names that appear in `IndexProgress`. */
export type IndexPhase = 'scanning' | 'extracting' | 'done';

/** Progress snapshot returned / emitted during indexing. */
export interface IndexProgress {
    phase:               IndexPhase;
    filesScanned:        number;
    /** Number of files whose mtime or sha1 changed since last index. */
    staleFilesCount?:    number;
    /** Total symbols extracted during Phase 2. */
    symbolsExtracted?:   number;
}

/** Options accepted by `indexWorkspace()`. */
export interface IndexOptions {
    backendUrl:       string;
    /** Maximum milliseconds to wait for Phase 1 to complete. Default 30 000. */
    phase1TimeoutMs?: number;
    /** Priority files to process first in Phase 2 (absolute paths). */
    priorityFiles?:   string[];
    /** Called on every significant progress change. */
    onProgress?:      (p: IndexProgress) => void;
}

// ---------------------------------------------------------------------------
// Module-level cancellation token
// ---------------------------------------------------------------------------

let _cancelRequested = false;

/**
 * Signal the currently running `indexWorkspace()` to stop Phase 2 at the
 * next opportunity.  Has no effect if no index is running.
 */
export function cancelCurrentIndex(): void {
    _cancelRequested = true;
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Index `workspaceRoot` in two phases.
 *
 * Returns a progress snapshot taken right after Phase 1 completes.
 * Phase 2 runs asynchronously (fire-and-forget).
 */
export async function indexWorkspace(
    workspaceRoot: string,
    db:            ConductorDb,
    options:       IndexOptions,
): Promise<IndexProgress> {
    _cancelRequested = false;

    const timeoutMs = options.phase1TimeoutMs ?? 30_000;
    const report    = (phase: IndexPhase, filesScanned: number): IndexProgress => {
        const p: IndexProgress = { phase, filesScanned };
        options.onProgress?.(p);
        return p;
    };

    report('scanning', 0);

    // Phase 1: scan with timeout.
    let scanError: unknown = null;
    try {
        await Promise.race([
            scanWorkspaceV1(workspaceRoot, db),
            new Promise<void>((_, reject) =>
                setTimeout(() => reject(new Error('Phase 1 timeout')), timeoutMs),
            ),
        ]);
    } catch (err) {
        scanError = err;
    }

    const allFiles    = db.getAllFiles();
    const filesScanned = allFiles.length;
    const snap        = report('extracting', filesScanned);

    // Phase 2: extract symbols for stale files (fire-and-forget).
    const staleFiles = db.getFilesNeedingReindex();
    void _runPhase2(workspaceRoot, db, staleFiles, options).then(() => {
        report('done', filesScanned);
    }).catch(() => {
        /* swallow — Phase 2 errors are non-fatal */
    });

    // If Phase 1 timed out, return a valid (possibly partial) snapshot.
    void scanError; // acknowledged
    return snap;
}

/**
 * Re-index a single file: extract its symbols and update the DB.
 *
 * @param workspaceRoot - Absolute root of the workspace (used to compute relative path).
 * @param absPath       - Absolute path to the file to re-index.
 * @param db            - Local metadata database.
 * @returns The number of symbols extracted.
 */
export async function reindexSingleFile(
    workspaceRoot: string,
    absPath:       string,
    db:            ConductorDb,
): Promise<number> {
    const relPath = path.relative(workspaceRoot, absPath);
    return _extractAndStoreSymbols(absPath, relPath, db);
}

// ---------------------------------------------------------------------------
// Private helpers
// ---------------------------------------------------------------------------

async function _runPhase2(
    workspaceRoot: string,
    db:            ConductorDb,
    files:         ReturnType<ConductorDb['getFilesNeedingReindex']>,
    _options:      IndexOptions,
): Promise<number> {
    let total = 0;
    for (const file of files) {
        if (_cancelRequested) { break; }
        const absPath = path.join(workspaceRoot, file.path);
        total += await _extractAndStoreSymbols(absPath, file.path, db);
    }
    return total;
}

/**
 * Extract symbols from `absPath` and persist them to the DB under `relPath`.
 * @returns Number of symbols stored (0 on error / unsupported extension).
 */
async function _extractAndStoreSymbols(
    absPath: string,
    relPath: string,
    db:      ConductorDb,
): Promise<number> {
    try {
        const { symbols } = extractSymbols(absPath);

        const rows: SymbolRow[] = symbols.map(sym => ({
            id:         crypto.createHash('sha1').update(`${relPath}:${sym.name}:${sym.kind}`).digest('hex'),
            path:       relPath,
            name:       sym.name,
            kind:       sym.kind,
            start_line: sym.range.start.line,
            end_line:   sym.range.end.line,
            signature:  sym.signature,
        }));

        db.replaceSymbolsForFile(relPath, rows);
        return rows.length;
    } catch {
        /* skip unreadable / unparseable files */
        return 0;
    }
}

