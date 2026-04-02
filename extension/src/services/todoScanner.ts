/**
 * Workspace TODO Scanner
 *
 * Scans the workspace for structured TODO comments and returns them as a list.
 *
 * Supported formats (case-insensitive prefix):
 *   // TODO: task title
 *   // TODO_DESC: optional description (must immediately follow the TODO line)
 *
 *   # TODO: task title
 *   # TODO_DESC: optional description
 *
 *   -- TODO: task title   (SQL)
 *   -- TODO_DESC: description
 *
 * The scanner also detects unstructured TODOs (e.g. "// TODO fix this") but
 * only parses a title from them (no description support).
 */
import * as vscode from 'vscode';
import * as fs from 'fs';
import * as path from 'path';
import * as crypto from 'crypto';

/** Structured ticket tag: {provider:KEY} e.g. {jira:DEV-123} */
const TICKET_TAG_RE = /\{(\w+):([^}]+)\}/;
/** Bare Jira-style ticket key: PROJECT-123 */
const TICKET_KEY_RE = /\b[A-Z][A-Z0-9]+-\d+\b/;

export interface WorkspaceTodo {
    /** Stable ID derived from file path + line number. */
    id: string;
    /** Absolute path to the source file. */
    filePath: string;
    /** Workspace-relative path for display. */
    relativePath: string;
    /** 1-based line number of the TODO marker. */
    lineNumber: number;
    /** Parsed task title. */
    title: string;
    /** Parsed description (optional). */
    description?: string;
    /** 1-based line of the TODO_DESC comment, if present. */
    descriptionLine?: number;
    /** Comment prefix detected on the TODO line (e.g. '//', '#', '--'). */
    commentPrefix: string;
    /** Ticket key extracted from title or description (e.g. 'DEV-123'). */
    ticketKey?: string;
}

/** File extensions to scan (text-based source files). */
const SCAN_EXTENSIONS = new Set([
    '.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs',
    '.py', '.java', '.go', '.cs', '.cpp', '.c', '.h', '.hpp',
    '.rs', '.rb', '.php', '.swift', '.kt', '.scala',
    '.sh', '.bash', '.zsh', '.fish',
    '.yaml', '.yml', '.toml', '.ini',
    '.html', '.vue', '.svelte',
    '.css', '.scss', '.less',
    '.sql',
]);

/** Directory segments to skip during traversal. */
const SKIP_DIRS = new Set([
    'node_modules', '.venv', 'venv', '.git', 'out', 'dist', 'build',
    '__pycache__', '.mypy_cache', '.pytest_cache', '.tox',
    'coverage', '.nyc_output', 'target', 'bin', 'obj',
]);

/** Regex to match a TODO comment line.
 *  Captures: (1) comment prefix, (2) title text.
 */
const TODO_RE = /^(\s*(?:\/\/|#|--|;;|\/\*)\s*)TODO:?\s+(.+)/i;

/** Regex to match a TODO_DESC line immediately following a TODO line.
 *  Captures: (1) comment prefix, (2) description text.
 */
const TODO_DESC_RE = /^(\s*(?:\/\/|#|--|;;|\/\*)\s*)TODO_DESC:?\s+(.*)/i;

function makeId(filePath: string, lineNumber: number): string {
    return crypto
        .createHash('sha1')
        .update(`${filePath}:${lineNumber}`)
        .digest('hex')
        .slice(0, 12);
}

/** Return the workspace-relative path, or the basename if not resolvable. */
function toRelative(absPath: string): string {
    const folders = vscode.workspace.workspaceFolders;
    if (!folders) return path.basename(absPath);
    for (const folder of folders) {
        const root = folder.uri.fsPath;
        if (absPath.startsWith(root)) {
            return absPath.slice(root.length).replace(/^[\\/]/, '');
        }
    }
    return path.basename(absPath);
}

/** Recursively collect scannable file paths under `dir`. */
function collectFiles(dir: string, results: string[]): void {
    let entries: fs.Dirent[];
    try {
        entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
        return;
    }
    for (const entry of entries) {
        if (entry.name.startsWith('.') && entry.name !== '.github') continue;
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) {
            if (!SKIP_DIRS.has(entry.name)) collectFiles(full, results);
        } else if (entry.isFile()) {
            const ext = path.extname(entry.name).toLowerCase();
            if (SCAN_EXTENSIONS.has(ext)) results.push(full);
        }
    }
}

/** Parse TODOs from a single file's text content. */
function parseFileTodos(filePath: string, content: string): WorkspaceTodo[] {
    const lines = content.split('\n');
    const todos: WorkspaceTodo[] = [];
    const relativePath = toRelative(filePath);

    for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        const match = TODO_RE.exec(line);
        if (!match) continue;

        const prefixRaw = match[1].trim(); // e.g. '//' or '#'
        // Normalise: strip leading whitespace and trailing space from prefix
        const commentPrefix = prefixRaw.replace(/\s+$/, '');
        const title = match[2].trim();
        if (!title) continue;

        // Check next line for optional TODO_DESC
        let description: string | undefined;
        let descriptionLine: number | undefined;
        if (i + 1 < lines.length) {
            const nextMatch = TODO_DESC_RE.exec(lines[i + 1]);
            if (nextMatch) {
                description = nextMatch[2].trim();
                descriptionLine = i + 2; // 1-based
            }
        }

        // Extract ticket key: prefer structured {jira:DEV-123} tag, fall back to bare DEV-123
        const ticketSource = description || title;
        const tagMatch = TICKET_TAG_RE.exec(ticketSource);
        const bareMatch = TICKET_KEY_RE.exec(ticketSource);
        const ticketKey = tagMatch ? tagMatch[2] : bareMatch ? bareMatch[0] : undefined;

        todos.push({
            id: makeId(filePath, i + 1),
            filePath,
            relativePath,
            lineNumber: i + 1,
            title,
            description,
            descriptionLine,
            commentPrefix,
            ticketKey,
        });
    }
    return todos;
}

/**
 * Scan the entire workspace for structured TODO comments.
 * Returns up to 500 results sorted by relativePath then lineNumber.
 */
export async function scanWorkspaceTodos(): Promise<WorkspaceTodo[]> {
    const folders = vscode.workspace.workspaceFolders;
    if (!folders || folders.length === 0) return [];

    const allFiles: string[] = [];
    for (const folder of folders) {
        collectFiles(folder.uri.fsPath, allFiles);
    }

    const results: WorkspaceTodo[] = [];
    for (const filePath of allFiles) {
        let content: string;
        try {
            content = fs.readFileSync(filePath, 'utf8');
        } catch {
            continue;
        }
        results.push(...parseFileTodos(filePath, content));
        if (results.length >= 500) break;
    }

    results.sort((a, b) =>
        a.relativePath.localeCompare(b.relativePath) || a.lineNumber - b.lineNumber,
    );
    return results;
}

export interface UpdateTodoPayload {
    filePath: string;
    lineNumber: number;           // 1-based TODO line
    newTitle: string;
    newDescription: string;       // empty string = remove desc
    descriptionLine?: number;     // 1-based, if a desc line already exists
    commentPrefix: string;        // e.g. '//', '#'
}

/**
 * Write updated title/description back to the source file.
 * Returns true on success.
 */
export function updateWorkspaceTodoInFile(payload: UpdateTodoPayload): boolean {
    const { filePath, lineNumber, newTitle, newDescription, descriptionLine, commentPrefix } = payload;
    let content: string;
    try {
        content = fs.readFileSync(filePath, 'utf8');
    } catch {
        return false;
    }

    const lines = content.split('\n');
    const todoIdx = lineNumber - 1; // 0-based

    if (todoIdx < 0 || todoIdx >= lines.length) return false;

    // Preserve original indentation
    const indentMatch = /^(\s*)/.exec(lines[todoIdx]);
    const indent = indentMatch ? indentMatch[1] : '';

    // Rebuild the TODO line
    lines[todoIdx] = `${indent}${commentPrefix} TODO: ${newTitle}`;

    if (descriptionLine !== undefined && descriptionLine > 0) {
        const descIdx = descriptionLine - 1;
        if (newDescription) {
            lines[descIdx] = `${indent}${commentPrefix} TODO_DESC: ${newDescription}`;
        } else {
            // Remove the description line
            lines.splice(descIdx, 1);
        }
    } else if (newDescription) {
        // Insert a new TODO_DESC line right after the TODO line
        lines.splice(todoIdx + 1, 0, `${indent}${commentPrefix} TODO_DESC: ${newDescription}`);
    }

    try {
        fs.writeFileSync(filePath, lines.join('\n'), 'utf8');
        return true;
    } catch {
        return false;
    }
}
