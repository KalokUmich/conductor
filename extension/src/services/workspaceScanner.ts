/**
 * Fast workspace scanner for Conductor V1.
 *
 * Traverses a workspace tree, detects source-file languages by extension,
 * computes per-file metadata (relative path, size, mtime, sha1), and upserts
 * the results into the Conductor SQLite database in a single transaction.
 *
 * Incremental: files whose mtime is unchanged reuse the existing sha1 so
 * subsequent scans avoid re-reading unmodified content.
 *
 * Ignored directories: .git, node_modules, dist, build, out, target
 *
 * @module services/workspaceScanner
 */

import * as fs from 'fs/promises';
import * as path from 'path';
import * as crypto from 'crypto';

import { ConductorDb, FileMeta } from './conductorDb';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const IGNORED_DIRS = new Set([
    // VCS
    '.git',
    // Conductor own cache
    '.conductor',
    // JS/TS build artefacts
    'node_modules',
    'dist',
    'build',
    'out',
    // JVM
    'target',
    // Python virtual-envs and caches (the biggest offenders for false file counts)
    '.venv',
    'venv',
    'env',
    '.env',
    '__pycache__',
    '.mypy_cache',
    '.pytest_cache',
    '.tox',
    'site-packages',
    // Coverage / misc
    '.coverage',
    'htmlcov',
    '.cache',
    // IDE
    '.idea',
    '.vscode',
]);

/** Map from file extension to canonical language name. */
export const LANG_MAP: Record<string, string> = {
    '.ts': 'typescript',
    '.tsx': 'typescript',
    '.js': 'javascript',
    '.jsx': 'javascript',
    '.py': 'python',
    '.java': 'java',
};

/** Number of concurrent I/O operations during stat and read passes. */
const CONCURRENCY = 64;

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

/**
 * Collect absolute paths of all source files under `dir`.
 * Directories listed in IGNORED_DIRS are skipped entirely.
 * Only files with a recognised language extension are returned.
 */
async function collectSourceFiles(dir: string): Promise<string[]> {
    const results: string[] = [];
    const stack: string[] = [dir];

    while (stack.length > 0) {
        const current = stack.pop()!;
        let entries;
        try {
            entries = await fs.readdir(current, { withFileTypes: true });
        } catch {
            continue; // skip unreadable directories
        }

        for (const entry of entries) {
            if (entry.isDirectory()) {
                if (!IGNORED_DIRS.has(entry.name)) {
                    stack.push(path.join(current, entry.name));
                }
            } else if (entry.isFile() && LANG_MAP[path.extname(entry.name)]) {
                results.push(path.join(current, entry.name));
            }
        }
    }

    return results;
}

/**
 * Run async factory functions with at most `limit` tasks in-flight at a time.
 * Results are returned in the same order as `tasks`.
 */
async function withConcurrency<T>(
    tasks: Array<() => Promise<T>>,
    limit: number,
): Promise<T[]> {
    if (tasks.length === 0) {
        return [];
    }
    const results: T[] = new Array(tasks.length);
    let next = 0;

    // Each worker grabs the next available index and runs until exhausted.
    // JavaScript's single-threaded event loop makes the `next++` increment safe.
    async function worker(): Promise<void> {
        while (next < tasks.length) {
            const i = next++;
            results[i] = await tasks[i]();
        }
    }

    await Promise.all(Array.from({ length: Math.min(limit, tasks.length) }, worker));
    return results;
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Recursively scan `workspaceRoot` for source files, collect metadata, and
 * upsert the results into `.conductor/cache.db`.
 *
 * The function is incremental: files whose mtime has not changed since the
 * last scan reuse their stored sha1 without re-reading the file, making
 * subsequent scans significantly faster.
 *
 * Performance target: < 2 seconds for 10 000 source files.
 *
 * @param workspaceRoot - Absolute path to the workspace root directory.
 * @param existingDb    - Optional pre-opened ConductorDb.  When provided,
 *                        the caller owns the lifecycle (open/close).  When
 *                        omitted the function opens its own db and closes
 *                        it before returning (original behaviour).
 */
export async function scanWorkspaceV1(
    workspaceRoot: string,
    existingDb?: ConductorDb,
): Promise<void> {
    // Ensure .conductor/ exists before ConductorDb tries to open the file.
    const conductorDir = path.join(workspaceRoot, '.conductor');
    await fs.mkdir(conductorDir, { recursive: true });

    const ownsDb = !existingDb;
    const db = existingDb ?? new ConductorDb(path.join(conductorDir, 'cache.db'));

    try {
        const absPaths = await collectSourceFiles(workspaceRoot);

        if (absPaths.length === 0) {
            return;
        }

        // Build a lookup of existing DB entries for incremental sha1 reuse.
        const existingMap = new Map<string, FileMeta>(
            db.getAllFiles().map(f => [f.path, f]),
        );

        // Stat all source files concurrently.
        const statResults = await withConcurrency(
            absPaths.map(absPath => async () => {
                const stat = await fs.stat(absPath);
                return { absPath, mtime: stat.mtimeMs, size: stat.size };
            }),
            CONCURRENCY,
        );

        // Partition into files we can reuse vs. files that need content reads.
        const toRead: Array<{ absPath: string; mtime: number; size: number }> = [];
        const rows: FileMeta[] = [];

        for (const { absPath, mtime, size } of statResults) {
            const relPath = path.relative(workspaceRoot, absPath);
            const lang = LANG_MAP[path.extname(absPath)] ?? '';
            const existing = existingMap.get(relPath);

            if (existing && existing.mtime === mtime && existing.sha1) {
                // mtime unchanged and sha1 already computed — reuse the row.
                rows.push({
                    path: relPath,
                    mtime,
                    size,
                    lang,
                    sha1: existing.sha1,
                    last_indexed_at: existing.last_indexed_at,
                });
            } else {
                toRead.push({ absPath, mtime, size });
            }
        }

        // Read and hash only the new or modified files.
        if (toRead.length > 0) {
            const hashed = await withConcurrency(
                toRead.map(({ absPath, mtime, size }) => async (): Promise<FileMeta> => {
                    const content = await fs.readFile(absPath);
                    const sha1 = crypto.createHash('sha1').update(content).digest('hex');
                    const relPath = path.relative(workspaceRoot, absPath);
                    const lang = LANG_MAP[path.extname(absPath)] ?? '';
                    return { path: relPath, mtime, size, lang, sha1, last_indexed_at: null };
                }),
                CONCURRENCY,
            );
            rows.push(...hashed);
        }

        // Single-transaction bulk upsert.
        db.upsertFiles(rows);
    } finally {
        if (ownsDb) {
            db.close();
        }
    }
}
