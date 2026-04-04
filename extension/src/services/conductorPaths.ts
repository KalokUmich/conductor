/**
 * Unified storage path resolution for Conductor.
 *
 * All persistent data lives under ~/.conductor/ (or CONDUCTOR_DATA_DIR override).
 * Data is partitioned by sanitized workspace path, similar to Claude Code's
 * ~/.claude/projects/{sanitized-cwd}/ pattern.
 *
 * Directory structure:
 *   ~/.conductor/
 *   ├── projects/{sanitized-workspace-path}/
 *   │   ├── chat_history/
 *   │   │   └── {session-id}.json      # Per-session message cache
 *   │   └── settings.json              # Per-project settings
 *   ├── sessions.json                   # Global session registry (all projects)
 *   └── settings.json                   # User-level settings
 *
 * @module services/conductorPaths
 */

import * as os from 'os';
import * as path from 'path';
import * as fs from 'fs';
import * as crypto from 'crypto';

const MAX_PATH_LENGTH = 200;

let _cachedRoot: string | null = null;

/**
 * Get the Conductor data root directory.
 * Resolution order:
 *   1. CONDUCTOR_DATA_DIR environment variable
 *   2. ~/.conductor/
 */
export function getConductorRoot(): string {
    if (_cachedRoot) return _cachedRoot;

    const envOverride = process.env.CONDUCTOR_DATA_DIR;
    if (envOverride) {
        _cachedRoot = envOverride;
    } else {
        _cachedRoot = path.join(os.homedir(), '.conductor');
    }

    // Ensure directory exists with secure permissions (user-only: 0700)
    if (!fs.existsSync(_cachedRoot)) {
        fs.mkdirSync(_cachedRoot, { recursive: true, mode: 0o700 });
    }

    // Ensure subdirectories exist
    const subdirs = ['projects', 'credentials', 'cache'];
    for (const sub of subdirs) {
        const subPath = path.join(_cachedRoot, sub);
        if (!fs.existsSync(subPath)) {
            fs.mkdirSync(subPath, { recursive: true, mode: sub === 'credentials' ? 0o700 : 0o755 });
        }
    }

    return _cachedRoot;
}

/**
 * Sanitize a filesystem path into a safe directory name.
 * Replaces path separators and special chars with hyphens.
 * Truncates long paths and appends a hash suffix.
 *
 * Examples:
 *   /home/kalok/abound-server  → -home-kalok-abound-server
 *   C:\Users\kalok\project     → C-Users-kalok-project
 */
export function sanitizePath(fsPath: string): string {
    // Normalize and replace special chars
    let sanitized = fsPath
        .replace(/\\/g, '-')      // Windows backslashes
        .replace(/\//g, '-')      // Unix slashes
        .replace(/:/g, '-')       // Drive letters (C:)
        .replace(/\s+/g, '-')     // Whitespace
        .replace(/[^a-zA-Z0-9._-]/g, '-')  // Other special chars
        .replace(/-+/g, '-');     // Collapse multiple hyphens

    // Truncate if too long, append hash for uniqueness
    if (sanitized.length > MAX_PATH_LENGTH) {
        const hash = crypto.createHash('sha256').update(fsPath).digest('hex').slice(0, 8);
        sanitized = sanitized.slice(0, MAX_PATH_LENGTH - 9) + '-' + hash;
    }

    return sanitized;
}

/**
 * Get the project-specific data directory for a workspace path.
 * Creates the directory if it doesn't exist.
 *
 * @param workspacePath - Absolute path to the workspace folder
 * @returns Path to ~/.conductor/projects/{sanitized}/
 */
export function getProjectDir(workspacePath: string): string {
    const sanitized = sanitizePath(workspacePath);
    const dir = path.join(getConductorRoot(), 'projects', sanitized);
    if (!fs.existsSync(dir)) {
        fs.mkdirSync(dir, { recursive: true });
    }
    return dir;
}

/**
 * Get the chat history directory for a workspace.
 *
 * @param workspacePath - Absolute path to the workspace folder
 * @returns Path to ~/.conductor/projects/{sanitized}/chat_history/
 */
export function getChatHistoryDir(workspacePath: string): string {
    const dir = path.join(getProjectDir(workspacePath), 'chat_history');
    if (!fs.existsSync(dir)) {
        fs.mkdirSync(dir, { recursive: true });
    }
    return dir;
}

/**
 * Get the global sessions file path.
 * @returns Path to ~/.conductor/sessions.json
 */
export function getSessionsFilePath(): string {
    return path.join(getConductorRoot(), 'sessions.json');
}

/**
 * Get the LLM logs directory for a workspace.
 *
 * @param workspacePath - Absolute path to the workspace folder
 * @returns Path to ~/.conductor/projects/{sanitized}/llm_logs/
 */
export function getLLMLogsDir(workspacePath: string): string {
    const dir = path.join(getProjectDir(workspacePath), 'llm_logs');
    if (!fs.existsSync(dir)) {
        fs.mkdirSync(dir, { recursive: true });
    }
    return dir;
}

/**
 * Get the credentials directory.
 * @returns Path to ~/.conductor/credentials/
 */
export function getCredentialsDir(): string {
    return path.join(getConductorRoot(), 'credentials');
}

/**
 * Get the user settings file path.
 * @returns Path to ~/.conductor/settings.json
 */
export function getUserSettingsPath(): string {
    return path.join(getConductorRoot(), 'settings.json');
}
