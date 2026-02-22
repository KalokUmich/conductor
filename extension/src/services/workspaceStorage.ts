/**
 * Conductor workspace storage initialization.
 *
 * Creates and maintains the `.conductor/` directory at the workspace root,
 * which holds local configuration, file metadata, and vector storage for
 * the context enricher.
 *
 * Idempotent — safe to call on every session start.
 *
 * @module services/workspaceStorage
 */

import * as fs from 'fs/promises';
import * as path from 'path';

import { ConductorDb } from './conductorDb';

// ---------------------------------------------------------------------------
// Workspace configuration types
// ---------------------------------------------------------------------------

/**
 * Shape of `.conductor/config.json`.
 *
 * All fields are optional when reading from disk — missing fields fall back to
 * the values in `DEFAULT_WORKSPACE_CONFIG`.  This means old config files
 * written before a field was added continue to work without manual migration.
 */
/**
 * Settings stored in `.conductor/config.json` — extension-side tuning knobs
 * that do NOT duplicate backend configuration.
 *
 * Embedding model and dimension are intentionally absent here.
 * They live in `conductor.settings.yaml` (backend) and are fetched at
 * runtime via `GET /embeddings/config` so there is a single source of truth.
 */
export interface WorkspaceConfig {
    /** Directory names to skip during workspace scanning. */
    ignorePatterns: string[];
    /**
     * Maximum number of LSP reference entries passed to the ranker.
     * Maps to `maxReferences` in `relevanceRanker.rank()`.
     */
    maxRelated: number;
    /**
     * Maximum number of distinct files in the ranked context output.
     * Maps to `maxFiles` in `relevanceRanker.rank()`.
     */
    maxContextFiles: number;
    /**
     * Top-K value passed to `VectorIndex.search()` when running semantic search.
     */
    semanticTopK: number;
}

/** Canonical defaults — every field has a safe value out of the box. */
export const DEFAULT_WORKSPACE_CONFIG: WorkspaceConfig = {
    ignorePatterns: [
        'node_modules',
        '.venv',
        '__pycache__',
        'dist',
        'out',
        '.git',
    ],
    maxRelated:      8,
    maxContextFiles: 15,
    semanticTopK:    20,
};

/** @deprecated Use `DEFAULT_WORKSPACE_CONFIG` — kept for backward compatibility. */
const DEFAULT_CONFIG = DEFAULT_WORKSPACE_CONFIG;

// ---------------------------------------------------------------------------
// Embedding configuration (fetched from backend, not stored locally)
// ---------------------------------------------------------------------------

/** Embedding configuration returned by `GET /embeddings/config`. */
export interface EmbeddingConfig {
    model:    string;
    dim:      number;
    provider: string;
}

/** Safe fallback used when the backend is unreachable at startup. */
export const DEFAULT_EMBEDDING_CONFIG: EmbeddingConfig = {
    model:    'cohere.embed-english-v3',
    dim:      1024,
    provider: 'bedrock',
};

/**
 * Fetch the active embedding configuration from the backend.
 *
 * The backend reads `conductor.settings.yaml`, so this is the single source
 * of truth.  Falls back to `DEFAULT_EMBEDDING_CONFIG` when the backend is
 * not reachable (e.g. first run before hosting starts).
 *
 * @param backendUrl  Base URL of the Conductor backend.
 */
export async function fetchEmbeddingConfig(backendUrl: string): Promise<EmbeddingConfig> {
    try {
        const response = await fetch(`${backendUrl}/embeddings/config`);
        if (!response.ok) {
            console.warn(`[ConductorStorage] GET /embeddings/config returned ${response.status} — using defaults`);
            return { ...DEFAULT_EMBEDDING_CONFIG };
        }
        const data = await response.json() as Partial<EmbeddingConfig>;
        return {
            model:    data.model    ?? DEFAULT_EMBEDDING_CONFIG.model,
            dim:      data.dim      ?? DEFAULT_EMBEDDING_CONFIG.dim,
            provider: data.provider ?? DEFAULT_EMBEDDING_CONFIG.provider,
        };
    } catch (err) {
        console.warn('[ConductorStorage] Could not fetch embedding config — using defaults:', err);
        return { ...DEFAULT_EMBEDDING_CONFIG };
    }
}

/** Default contents for `.conductor/file_meta.json`. */
const DEFAULT_FILE_META = {
    files: [],
};

// ---------------------------------------------------------------------------
// Config loading
// ---------------------------------------------------------------------------

/**
 * Read `.conductor/config.json` from the workspace root and merge it with
 * `DEFAULT_WORKSPACE_CONFIG`.
 *
 * Missing fields in the file fall back to the default value — forward
 * compatibility is preserved when new fields are added.  If the file does not
 * exist or cannot be parsed, the full default config is returned.
 *
 * @param workspaceRoot  Absolute path to the workspace root folder.
 */
export async function loadWorkspaceConfig(workspaceRoot: string): Promise<WorkspaceConfig> {
    const configPath = path.join(workspaceRoot, '.conductor', 'config.json');
    try {
        const raw    = await fs.readFile(configPath, 'utf-8');
        const parsed = JSON.parse(raw) as Partial<WorkspaceConfig>;
        return { ...DEFAULT_WORKSPACE_CONFIG, ...parsed };
    } catch {
        return { ...DEFAULT_WORKSPACE_CONFIG };
    }
}

// ---------------------------------------------------------------------------
// Storage initialisation
// ---------------------------------------------------------------------------

/**
 * Ensure a JSON file exists at `filePath`. If it already exists, skip.
 * Otherwise write `defaultContent` as pretty-printed JSON.
 */
async function ensureJsonFile(
    filePath: string,
    defaultContent: unknown,
): Promise<boolean> {
    try {
        await fs.access(filePath);
        return false; // already exists
    } catch {
        await fs.writeFile(
            filePath,
            JSON.stringify(defaultContent, null, 2) + '\n',
            'utf-8',
        );
        return true; // created
    }
}

/**
 * Initialize the `.conductor/` workspace storage directory.
 *
 * Creates the following structure (only missing pieces are created):
 * ```
 * .conductor/
 * ├── config.json       — enricher settings  (see WorkspaceConfig)
 * ├── file_meta.json    — tracked file metadata
 * └── vectors/          — vector embeddings cache
 * ```
 *
 * @param workspaceRoot - Absolute path to the workspace root folder.
 * @returns An opened `ConductorDb` instance backed by `.conductor/cache.db`.
 */
export async function initConductorWorkspaceStorage(
    workspaceRoot: string,
): Promise<ConductorDb> {
    const conductorDir  = path.join(workspaceRoot, '.conductor');
    const vectorsDir    = path.join(conductorDir, 'vectors');
    const configPath    = path.join(conductorDir, 'config.json');
    const fileMetaPath  = path.join(conductorDir, 'file_meta.json');

    // Ensure .conductor/ and .conductor/vectors/ exist
    await fs.mkdir(vectorsDir, { recursive: true });
    console.log(`[ConductorStorage] Ensured directory: ${conductorDir}`);

    // Write default files if missing
    const configCreated = await ensureJsonFile(configPath, DEFAULT_CONFIG);
    if (configCreated) {
        console.log('[ConductorStorage] Created config.json with defaults');
    }

    const metaCreated = await ensureJsonFile(fileMetaPath, DEFAULT_FILE_META);
    if (metaCreated) {
        console.log('[ConductorStorage] Created file_meta.json with defaults');
    }

    // Ensure .conductor/ is in the workspace .gitignore
    await ensureGitignoreEntry(workspaceRoot);

    // Open the SQLite database
    const dbPath = path.join(conductorDir, 'cache.db');
    const db     = new ConductorDb(dbPath);
    console.log(`[ConductorStorage] Opened cache DB: ${dbPath}`);

    return db;
}

/**
 * Hard-reset the workspace index:
 * 1. Close the existing database connection.
 * 2. Delete `cache.db`, `cache.db-shm`, `cache.db-wal` and the `vectors/` directory.
 * 3. Open and return a fresh `ConductorDb` with an empty schema.
 *
 * Config files (`config.json`, `file_meta.json`) are preserved.
 */
export async function resetWorkspaceDb(
    workspaceRoot: string,
    oldDb: ConductorDb,
): Promise<ConductorDb> {
    const conductorDir = path.join(workspaceRoot, '.conductor');
    const dbPath       = path.join(conductorDir, 'cache.db');
    const vectorsDir   = path.join(conductorDir, 'vectors');

    // 1. Close the open connection before touching the files.
    oldDb.close();

    // 2. Remove SQLite files (WAL artefacts included).
    for (const suffix of ['', '-shm', '-wal']) {
        await fs.rm(dbPath + suffix, { force: true });
    }

    // 3. Clear vectors directory contents (keep the directory itself).
    try {
        const entries = await fs.readdir(vectorsDir);
        await Promise.all(entries.map(e => fs.rm(path.join(vectorsDir, e), { force: true, recursive: true })));
    } catch { /* vectors dir might not exist */ }

    // 4. Open a fresh database (schema is recreated by the ConductorDb constructor).
    console.log(`[ConductorStorage] Hard-reset complete, reopening: ${dbPath}`);
    return new ConductorDb(dbPath);
}

/**
 * Append `.conductor/` to the workspace `.gitignore` if not already present.
 * Creates the `.gitignore` file if it doesn't exist.
 */
async function ensureGitignoreEntry(workspaceRoot: string): Promise<void> {
    const gitignorePath = path.join(workspaceRoot, '.gitignore');
    const entry = '.conductor/';

    let content = '';
    try {
        content = await fs.readFile(gitignorePath, 'utf-8');
    } catch {
        // .gitignore doesn't exist — we'll create it
    }

    // Check if the entry is already present (exact line match)
    const lines = content.split('\n');
    if (lines.some(line => line.trim() === entry)) {
        return;
    }

    // Append with a trailing newline
    const suffix = content.length > 0 && !content.endsWith('\n') ? '\n' : '';
    await fs.writeFile(
        gitignorePath,
        content + suffix + entry + '\n',
        'utf-8',
    );
    console.log('[ConductorStorage] Added .conductor/ to .gitignore');
}
