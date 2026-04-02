/**
 * Conductor workspace storage initialization.
 *
 * Creates and maintains the `.conductor/` directory at the workspace root,
 * which holds local configuration (config.json).  The repo graph and symbol
 * index are managed separately by repoGraphBuilder and the backend.
 *
 * Idempotent — safe to call on every session start.
 *
 * @module services/workspaceStorage
 */

import * as fs from 'fs/promises';
import * as path from 'path';

// ---------------------------------------------------------------------------
// Workspace configuration types
// ---------------------------------------------------------------------------

/**
 * Settings stored in `.conductor/config.json` — extension-side tuning knobs.
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
    /** Top-K cap for ranked context results. */
    semanticTopK: number;
    /** Enable TODO ↔ ticket system bidirectional sync. */
    ticketSyncEnabled: boolean;
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
    ticketSyncEnabled: true,
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
 * └── config.json       — enricher settings  (see WorkspaceConfig)
 * ```
 *
 * Repo graph (`repo_graph.json`) and symbol index (`symbol_index.json`)
 * are created on demand by their respective builders.
 *
 * @param workspaceRoot - Absolute path to the workspace root folder.
 */
export async function initConductorWorkspaceStorage(
    workspaceRoot: string,
): Promise<void> {
    const conductorDir = path.join(workspaceRoot, '.conductor');
    const configPath   = path.join(conductorDir, 'config.json');

    // Ensure .conductor/ exists
    await fs.mkdir(conductorDir, { recursive: true });
    console.log(`[ConductorStorage] Ensured directory: ${conductorDir}`);

    // Write default config if missing
    const configCreated = await ensureJsonFile(configPath, DEFAULT_WORKSPACE_CONFIG);
    if (configCreated) {
        console.log('[ConductorStorage] Created config.json with defaults');
    }

    // Ensure .conductor/ is in the workspace .gitignore
    await ensureGitignoreEntry(workspaceRoot);
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
