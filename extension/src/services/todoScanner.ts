/**
 * Workspace TODO Scanner
 *
 * Scans the workspace for structured TODO comments and returns them as a list.
 *
 * Supported formats (case-insensitive prefix):
 *   // TODO {jira:DEV-123#1}: task title
 *   // TODO_DESC: optional description
 *   //+ continuation line (belongs to preceding TODO_DESC)
 *
 *   # TODO {jira:DEV-123#2|after:1}: task title
 *   # TODO_DESC: description
 *   #+ continuation
 *
 *   -- TODO {jira:DEV-456#1|blocked:DEV-123}: task title   (SQL)
 *   -- TODO_DESC: description
 *
 * Legacy formats still supported:
 *   // TODO: task title
 *   // TODO_DESC: description
 *
 * Tag syntax: {jira:[PARENT>]TICKET[#N][|after:N[,N]][|blocked:TICKET[,TICKET]]}
 */
import * as vscode from 'vscode';
import * as fs from 'fs';
import * as path from 'path';
import * as crypto from 'crypto';

/** Structured ticket tag: {provider:KEY} e.g. {jira:DEV-123} */
const TICKET_TAG_RE = /\{(\w+):([^}]+)\}/;
/** Bare Jira-style ticket key: PROJECT-123 */
const TICKET_KEY_RE = /\b[A-Z][A-Z0-9]+-\d+\b/;

/**
 * Parse inner content of a {jira:...} tag.
 * Grammar: [PARENT>]TICKET[#N][|after:N[,N]][|blocked:TICKET[,TICKET]]
 */
const JIRA_TAG_INNER_RE = /^(?:([A-Z][A-Z0-9]+-\d+)>)?([A-Z][A-Z0-9]+-\d+)(?:#(\d+))?(?:\|(.+))?$/;

interface JiraTagParsed {
    ticketKey: string;
    changeNumber?: number;
    afterDeps?: number[];
    blockedBy?: string[];
    parentTicket?: string;
}

function parseJiraTagInner(inner: string): JiraTagParsed | null {
    const m = JIRA_TAG_INNER_RE.exec(inner);
    if (!m) return null;
    const result: JiraTagParsed = { ticketKey: m[2] };
    if (m[1]) result.parentTicket = m[1];
    if (m[3]) result.changeNumber = parseInt(m[3], 10);
    if (m[4]) {
        for (const seg of m[4].split('|')) {
            if (seg.startsWith('after:')) {
                result.afterDeps = seg.slice(6).split(',').map(n => parseInt(n, 10));
            } else if (seg.startsWith('blocked:')) {
                result.blockedBy = seg.slice(8).split(',');
            }
        }
    }
    return result;
}

export interface WorkspaceTodo {
    /** Stable ID derived from file path + line number. */
    id: string;
    /** Absolute path to the source file. */
    filePath: string;
    /** Workspace-relative path for display. */
    relativePath: string;
    /** 1-based line number of the TODO marker. */
    lineNumber: number;
    /** Parsed task title (tag stripped). */
    title: string;
    /** Parsed description (optional, includes continuation lines joined with \n). */
    description?: string;
    /** 1-based line of the TODO_DESC comment, if present. */
    descriptionLine?: number;
    /** Comment prefix detected on the TODO line (e.g. '//', '#', '--'). */
    commentPrefix: string;
    /** Ticket key extracted from tag or bare key (e.g. 'DEV-123'). */
    ticketKey?: string;
    /** Change number within the ticket (e.g. 2 from {jira:DEV-10424#2}). */
    changeNumber?: number;
    /** Intra-ticket dependencies: change numbers that must complete first. */
    afterDeps?: number[];
    /** Cross-ticket blockers: ticket keys that must have all TODOs resolved first. */
    blockedBy?: string[];
    /** Parent ticket key if using PARENT>CHILD syntax. */
    parentTicket?: string;
    /** 1-based line of the last line belonging to this TODO block (including //+ continuations). */
    blockEndLine?: number;
    /** Original {jira:...} tag string for write-back preservation. */
    rawTag?: string;
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

/** Continuation line: //+ text (+ immediately follows comment prefix, no space before +). */
const CONTINUATION_RE = /^(\s*(?:\/\/|#|--|;;|\/\*))\+\s?(.*)/;

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
        const commentPrefix = prefixRaw.replace(/\s+$/, '');
        const rawTitle = match[2].trim();
        if (!rawTitle) continue;

        // --- Extract structured {jira:...} tag from rawTitle ---
        let ticketKey: string | undefined;
        let changeNumber: number | undefined;
        let afterDeps: number[] | undefined;
        let blockedBy: string[] | undefined;
        let parentTicket: string | undefined;
        let rawTag: string | undefined;
        let title: string;

        const tagMatch = TICKET_TAG_RE.exec(rawTitle);
        if (tagMatch && tagMatch[1].toLowerCase() === 'jira') {
            rawTag = tagMatch[0]; // e.g. "{jira:DEV-10424#2|after:1}"
            const parsed = parseJiraTagInner(tagMatch[2]);
            if (parsed) {
                ticketKey = parsed.ticketKey;
                changeNumber = parsed.changeNumber;
                afterDeps = parsed.afterDeps;
                blockedBy = parsed.blockedBy;
                parentTicket = parsed.parentTicket;
            } else {
                // Tag present but inner parse failed — use raw content as ticketKey
                ticketKey = tagMatch[2];
            }
            // Strip tag from title: "{jira:...}: Build transaction ID" → "Build transaction ID"
            title = rawTitle.replace(tagMatch[0], '').replace(/^[:\s]+/, '').trim();
        } else {
            title = rawTitle;
        }

        // --- Check next line for optional TODO_DESC + continuation lines ---
        let description: string | undefined;
        let descriptionLine: number | undefined;
        let blockEndLine: number | undefined;

        if (i + 1 < lines.length) {
            const nextMatch = TODO_DESC_RE.exec(lines[i + 1]);
            if (nextMatch) {
                description = nextMatch[2].trim();
                descriptionLine = i + 2; // 1-based
                blockEndLine = i + 2;

                // Collect //+ continuation lines
                let j = i + 2; // 0-based index of line after TODO_DESC
                while (j < lines.length) {
                    const contMatch = CONTINUATION_RE.exec(lines[j]);
                    if (!contMatch) break;
                    description += '\n' + contMatch[2];
                    blockEndLine = j + 1; // 1-based
                    j++;
                }
            }
        }

        // --- Fallback: extract ticket key from title/desc if no structured tag ---
        if (!ticketKey) {
            const ticketSource = description || title;
            const fallbackTag = TICKET_TAG_RE.exec(ticketSource);
            const fallbackBare = TICKET_KEY_RE.exec(ticketSource);
            ticketKey = fallbackTag ? fallbackTag[2] : fallbackBare ? fallbackBare[0] : undefined;
        }

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
            changeNumber,
            afterDeps,
            blockedBy,
            parentTicket,
            blockEndLine,
            rawTag,
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
    rawTag?: string;              // preserved {jira:...} tag to prepend
    blockEndLine?: number;        // 1-based last line of TODO block (for continuation cleanup)
}

/**
 * Write updated title/description back to the source file.
 * Returns true on success.
 */
export function updateWorkspaceTodoInFile(payload: UpdateTodoPayload): boolean {
    const { filePath, lineNumber, newTitle, newDescription, descriptionLine, commentPrefix, rawTag, blockEndLine } = payload;
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

    // Rebuild the TODO line — preserve {jira:...} tag if present
    if (rawTag) {
        lines[todoIdx] = `${indent}${commentPrefix} TODO ${rawTag}: ${newTitle}`;
    } else {
        lines[todoIdx] = `${indent}${commentPrefix} TODO: ${newTitle}`;
    }

    // Remove old description block (desc line through last continuation line)
    const descStart = descriptionLine !== undefined && descriptionLine > 0 ? descriptionLine - 1 : -1;
    const descEnd = blockEndLine !== undefined && blockEndLine > 0 ? blockEndLine - 1 : descStart;

    if (descStart >= 0) {
        const removeCount = descEnd - descStart + 1;
        lines.splice(descStart, removeCount);
    }

    // Insert new description (split on \n for continuations)
    if (newDescription) {
        const descLines = newDescription.split('\n');
        const insertAt = todoIdx + 1;
        // First line as TODO_DESC
        lines.splice(insertAt, 0, `${indent}${commentPrefix} TODO_DESC: ${descLines[0]}`);
        // Remaining lines as continuations
        for (let k = 1; k < descLines.length; k++) {
            lines.splice(insertAt + k, 0, `${indent}${commentPrefix}+ ${descLines[k]}`);
        }
    }

    try {
        fs.writeFileSync(filePath, lines.join('\n'), 'utf8');
        return true;
    } catch {
        return false;
    }
}
