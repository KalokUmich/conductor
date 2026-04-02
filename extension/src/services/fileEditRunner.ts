/**
 * File Edit/Write tool runners for local mode.
 *
 * Mirrors the Python backend's file_edit_tools.py with the same safety
 * checks: read-before-write, staleness, path safety, secret detection.
 *
 * @module services/fileEditRunner
 */

import * as fs from 'fs';
import * as path from 'path';
import * as crypto from 'crypto';

import type { ToolResult } from './toolTypes';

// ---------------------------------------------------------------------------
// Read state tracking
// ---------------------------------------------------------------------------

/** Maps absolute file path → { contentHash, mtime, readTime } */
const _readState = new Map<string, { hash: string; mtime: number; readTime: number }>();

export function recordFileRead(absPath: string, content: string): void {
    let mtime = 0;
    try { mtime = fs.statSync(absPath).mtimeMs; } catch { /* ignore */ }
    _readState.set(absPath, {
        hash: crypto.createHash('md5').update(content).digest('hex'),
        mtime,
        readTime: Date.now(),
    });
}

export function clearFileReadState(): void {
    _readState.clear();
}

// ---------------------------------------------------------------------------
// Path safety
// ---------------------------------------------------------------------------

const BLOCKED_DIRS = new Set([
    '.git', 'node_modules', '.venv', 'venv', '__pycache__',
    '.mypy_cache', '.pytest_cache', '.tox',
]);

const BLOCKED_FILES = new Set([
    '.env', '.env.local', '.env.production',
    '.gitconfig', '.bashrc', '.zshrc', '.profile',
]);

const SECRET_PATTERNS = [
    /(?:api[_-]?key|secret[_-]?key|password|token|credential)\s*[:=]\s*['"][^'"]{8,}/i,
    /-----BEGIN\s+(?:RSA\s+)?PRIVATE\s+KEY-----/i,
    /sk-[a-zA-Z0-9]{20,}/,
    /AKIA[0-9A-Z]{16}/,
];

function resolveSafe(workspace: string, filePath: string): { absPath: string; error?: string } {
    const wsRoot = path.resolve(workspace);
    const target = path.resolve(wsRoot, filePath);

    // Must be within workspace
    if (!target.startsWith(wsRoot + path.sep) && target !== wsRoot) {
        return { absPath: target, error: `Path escapes workspace: ${filePath}` };
    }

    // Check blocked directories
    const rel = path.relative(wsRoot, target);
    const parts = rel.split(path.sep);
    for (const part of parts.slice(0, -1)) {
        if (BLOCKED_DIRS.has(part)) {
            return { absPath: target, error: `Cannot edit files in ${part}/ directory` };
        }
    }

    // Check blocked files
    if (BLOCKED_FILES.has(path.basename(target))) {
        return { absPath: target, error: `Cannot edit protected file: ${path.basename(target)}` };
    }

    return { absPath: target };
}

function checkSecrets(content: string): string[] {
    const warnings: string[] = [];
    for (const pattern of SECRET_PATTERNS) {
        if (pattern.test(content)) {
            warnings.push(`Potential secret detected: ${pattern.source.slice(0, 40)}...`);
        }
    }
    return warnings;
}

function generateDiff(oldContent: string, newContent: string, filePath: string): string {
    const oldLines = oldContent.split('\n');
    const newLines = newContent.split('\n');

    // Simple unified diff (not full algorithm — just show changed sections)
    const lines: string[] = [`--- a/${filePath}`, `+++ b/${filePath}`];
    let i = 0, j = 0;
    while (i < oldLines.length || j < newLines.length) {
        if (i < oldLines.length && j < newLines.length && oldLines[i] === newLines[j]) {
            i++; j++;
        } else {
            // Find changed range
            const startI = i, startJ = j;
            // Advance until we find a matching line again
            while (i < oldLines.length && !newLines.slice(j).includes(oldLines[i])) i++;
            while (j < newLines.length && !oldLines.slice(startI).includes(newLines[j])) j++;

            lines.push(`@@ -${startI + 1},${i - startI} +${startJ + 1},${j - startJ} @@`);
            for (let k = startI; k < i; k++) lines.push(`-${oldLines[k]}`);
            for (let k = startJ; k < j; k++) lines.push(`+${newLines[k]}`);
        }
    }
    return lines.length > 2 ? lines.join('\n') : '';
}

// ---------------------------------------------------------------------------
// Tool implementations
// ---------------------------------------------------------------------------

export function file_edit(
    workspace: string,
    params: { path: string; old_string: string; new_string: string; replace_all?: boolean },
): ToolResult {
    const { path: filePath, old_string, new_string, replace_all = false } = params;

    if (!old_string) return { success: false, data: null, error: 'old_string cannot be empty' };
    if (old_string === new_string) return { success: false, data: null, error: 'old_string and new_string are identical' };

    const { absPath, error } = resolveSafe(workspace, filePath);
    if (error) return { success: false, data: null, error };

    if (!fs.existsSync(absPath)) return { success: false, data: null, error: `File not found: ${filePath}` };

    // Read-before-write
    if (!_readState.has(absPath)) {
        return { success: false, data: null, error: `File has not been read yet. Use read_file on '${filePath}' first.` };
    }

    // Staleness
    try {
        const currentMtime = fs.statSync(absPath).mtimeMs;
        const recorded = _readState.get(absPath)!;
        if (currentMtime > recorded.mtime + 500) {
            return { success: false, data: null, error: `File '${filePath}' has been modified since you last read it. Please read_file again.` };
        }
    } catch { /* ignore */ }

    const oldContent = fs.readFileSync(absPath, 'utf-8');
    const count = oldContent.split(old_string).length - 1;
    if (count === 0) {
        return { success: false, data: null, error: `old_string not found in '${filePath}'.` };
    }
    if (count > 1 && !replace_all) {
        return { success: false, data: null, error: `Found ${count} matches. Set replace_all=true or provide more context.` };
    }

    const newContent = replace_all
        ? oldContent.split(old_string).join(new_string)
        : oldContent.replace(old_string, new_string);

    const secretWarnings = checkSecrets(new_string);
    const diff = generateDiff(oldContent, newContent, filePath);

    fs.writeFileSync(absPath, newContent, 'utf-8');
    recordFileRead(absPath, newContent);

    return {
        success: true,
        data: {
            path: filePath,
            replacements: replace_all ? count : 1,
            diff,
            secret_warnings: secretWarnings,
            bytes_before: oldContent.length,
            bytes_after: newContent.length,
        },
    };
}

export function file_write(
    workspace: string,
    params: { path: string; content: string },
): ToolResult {
    const { path: filePath, content } = params;

    const { absPath, error } = resolveSafe(workspace, filePath);
    if (error) return { success: false, data: null, error };

    const isNew = !fs.existsSync(absPath);

    if (!isNew && !_readState.has(absPath)) {
        return { success: false, data: null, error: `File '${filePath}' already exists. Use read_file first before overwriting.` };
    }

    if (!isNew) {
        try {
            const currentMtime = fs.statSync(absPath).mtimeMs;
            const recorded = _readState.get(absPath)!;
            if (currentMtime > recorded.mtime + 500) {
                return { success: false, data: null, error: `File '${filePath}' has been modified since you last read it.` };
            }
        } catch { /* ignore */ }
    }

    const secretWarnings = checkSecrets(content);
    let diff = '';
    if (!isNew) {
        const oldContent = fs.readFileSync(absPath, 'utf-8');
        diff = generateDiff(oldContent, content, filePath);
    }

    const dir = path.dirname(absPath);
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(absPath, content, 'utf-8');
    recordFileRead(absPath, content);

    return {
        success: true,
        data: {
            path: filePath,
            action: isNew ? 'created' : 'overwritten',
            diff,
            secret_warnings: secretWarnings,
            bytes: content.length,
        },
    };
}
