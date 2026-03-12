/**
 * ConductorFileSystemProvider – vscode.FileSystemProvider for conductor:// URIs.
 *
 * Implements the VS Code virtual file-system API so that files inside a remote
 * Conductor workspace can be read, written, and deleted through the standard
 * VS Code file explorer and editor.
 *
 * URI scheme:  conductor://<workspaceId>/<path/to/file>
 *
 * All file-system operations are proxied to the backend WorkspaceClient via
 * dedicated REST endpoints that are NOT yet part of the WorkspaceClient class
 * (they are stubbed here for completeness and will be wired up in a follow-on
 * PR).
 *
 * The provider raises the standard vscode.FileSystemError variants so that VS
 * Code’s explorer shows meaningful error messages.
 *
 * @module services/conductorFileSystemProvider
 */

import * as vscode from 'vscode';

// ---------------------------------------------------------------------------
// Internal types
// ---------------------------------------------------------------------------

/** Cached directory listing entry. */
interface CachedEntry {
    type: vscode.FileType;
    name: string;
}

// ---------------------------------------------------------------------------
// Blocked path prefixes
// ---------------------------------------------------------------------------

/**
 * Top-level path segments that VS Code auto-probes when a workspace folder is
 * added (e.g. `.vscode/settings.json`, `.git/HEAD`).  The backend intentionally
 * blocks these with 404, so we short-circuit here to avoid unnecessary network
 * calls and noisy access-log entries.
 */
const _BLOCKED_PREFIXES = new Set([
    '.vscode', '.idea', '.devcontainer', 'node_modules', '.git',
]);

// ---------------------------------------------------------------------------
// ConductorFileSystemProvider
// ---------------------------------------------------------------------------

/**
 * Virtual file-system provider that maps conductor:// URIs to files inside a
 * remote Conductor workspace.
 *
 * Register it in your extension activate() function:
 * ```ts
 * const provider = new ConductorFileSystemProvider(backendBaseUrl);
 * context.subscriptions.push(
 *   vscode.workspace.registerFileSystemProvider('conductor', provider, {
 *     isCaseSensitive: true,
 *     isReadonly: false,
 *   })
 * );
 * ```
 */
export class ConductorFileSystemProvider implements vscode.FileSystemProvider {
    // -----------------------------------------------------------------------
    // FileSystemProvider required members
    // -----------------------------------------------------------------------

    private readonly _onDidChangeFile =
        new vscode.EventEmitter<vscode.FileChangeEvent[]>();
    readonly onDidChangeFile: vscode.Event<vscode.FileChangeEvent[]> =
        this._onDidChangeFile.event;

    // -----------------------------------------------------------------------
    // Internal state
    // -----------------------------------------------------------------------

    /** LRU-style cache of directory listings, keyed by URI string. */
    private readonly _directoryCache = new Map<string, CachedEntry[]>();

    /** Outstanding watch disposables, keyed by URI string. */
    private readonly _watches = new Map<string, vscode.Disposable>();

    private readonly _getBaseUrl: () => string;

    constructor(backendBaseUrl: string | (() => string)) {
        if (typeof backendBaseUrl === 'function') {
            this._getBaseUrl = () => backendBaseUrl().replace(/\/$/, '');
        } else {
            const fixed = backendBaseUrl.replace(/\/$/, '');
            this._getBaseUrl = () => fixed;
        }
    }

    // -----------------------------------------------------------------------
    // Watch
    // -----------------------------------------------------------------------

    /**
     * VS Code calls this to start watching a resource for changes.
     * We implement a simple polling watcher that fires onDidChangeFile every
     * `pollIntervalMs` milliseconds.
     */
    watch(
        uri: vscode.Uri,
        options: { recursive: boolean; excludes: string[] }
    ): vscode.Disposable {
        const key = uri.toString();
        // Avoid duplicate watchers for the same URI.
        if (this._watches.has(key)) {
            return this._watches.get(key)!;
        }

        const pollIntervalMs = 5_000;
        const timer = setInterval(() => {
            // Invalidate cache so the next readDirectory call hits the network.
            this._directoryCache.delete(key);
            this._onDidChangeFile.fire([{
                type: vscode.FileChangeType.Changed,
                uri,
            }]);
        }, pollIntervalMs);

        const disposable = new vscode.Disposable(() => {
            clearInterval(timer);
            this._watches.delete(key);
        });
        this._watches.set(key, disposable);
        return disposable;
    }

    // -----------------------------------------------------------------------
    // stat
    // -----------------------------------------------------------------------

    /**
     * Return metadata for a file or directory.
     *
     * @throws {vscode.FileSystemError.FileNotFound} when the backend returns 404.
     */
    async stat(uri: vscode.Uri): Promise<vscode.FileStat> {
        const { workspaceId, filePath } = this._parseUri(uri);

        // Short-circuit paths the backend intentionally blocks (avoids 404 noise).
        const firstSegment = filePath.split('/')[0];
        if (_BLOCKED_PREFIXES.has(firstSegment)) {
            throw vscode.FileSystemError.FileNotFound(uri);
        }

        const pathPart = filePath ? `/${this._encodePath(filePath)}` : '';
        const url = `${this._getBaseUrl()}/workspace/${encodeURIComponent(workspaceId)}/files${pathPart}/stat`;

        let res: Response;
        try {
            res = await fetch(url);
        } catch (err) {
            throw vscode.FileSystemError.Unavailable(uri);
        }

        if (res.status === 404) {
            throw vscode.FileSystemError.FileNotFound(uri);
        }
        if (!res.ok) {
            throw vscode.FileSystemError.Unavailable(uri);
        }

        const data = (await res.json()) as {
            type: 'file' | 'directory';
            size: number;
            ctime: number;
            mtime: number;
        };

        return {
            type: data.type === 'directory' ? vscode.FileType.Directory : vscode.FileType.File,
            ctime: data.ctime,
            mtime: data.mtime,
            size: data.size,
        };
    }

    // -----------------------------------------------------------------------
    // readDirectory
    // -----------------------------------------------------------------------

    /**
     * List the contents of a directory.
     *
     * Results are cached in memory; the cache is invalidated by the watcher
     * timer and by successful write / delete operations.
     *
     * @throws {vscode.FileSystemError.FileNotFound} when the backend returns 404.
     */
    async readDirectory(uri: vscode.Uri): Promise<[string, vscode.FileType][]> {
        const { workspaceId, filePath } = this._parseUri(uri);

        // Short-circuit blocked paths.
        const firstSegment = filePath.split('/')[0];
        if (firstSegment && _BLOCKED_PREFIXES.has(firstSegment)) {
            throw vscode.FileSystemError.FileNotFound(uri);
        }

        const key = uri.toString();
        if (this._directoryCache.has(key)) {
            const cached = this._directoryCache.get(key)!;
            return cached.map((e) => [e.name, e.type]);
        }

        const pathPart = filePath ? `/${this._encodePath(filePath)}` : '';
        const url = `${this._getBaseUrl()}/workspace/${encodeURIComponent(workspaceId)}/files${pathPart}`;

        let res: Response;
        try {
            res = await fetch(url);
        } catch {
            throw vscode.FileSystemError.Unavailable(uri);
        }

        if (res.status === 404) {
            throw vscode.FileSystemError.FileNotFound(uri);
        }
        if (!res.ok) {
            throw vscode.FileSystemError.Unavailable(uri);
        }

        const items = (await res.json()) as Array<{ name: string; type: 'file' | 'directory' }>;
        const entries: CachedEntry[] = items.map((item) => ({
            name: item.name,
            type: item.type === 'directory' ? vscode.FileType.Directory : vscode.FileType.File,
        }));

        this._directoryCache.set(key, entries);
        return entries.map((e) => [e.name, e.type]);
    }

    // -----------------------------------------------------------------------
    // readFile
    // -----------------------------------------------------------------------

    /**
     * Read the entire content of a file as a Uint8Array.
     *
     * @throws {vscode.FileSystemError.FileNotFound} when the backend returns 404.
     */
    async readFile(uri: vscode.Uri): Promise<Uint8Array> {
        const { workspaceId, filePath } = this._parseUri(uri);

        // Short-circuit blocked paths.
        const firstSegment = filePath.split('/')[0];
        if (_BLOCKED_PREFIXES.has(firstSegment)) {
            throw vscode.FileSystemError.FileNotFound(uri);
        }

        const url = `${this._getBaseUrl()}/workspace/${encodeURIComponent(workspaceId)}/files/${this._encodePath(filePath)}/content`;

        let res: Response;
        try {
            res = await fetch(url);
        } catch {
            throw vscode.FileSystemError.Unavailable(uri);
        }

        if (res.status === 404) {
            throw vscode.FileSystemError.FileNotFound(uri);
        }
        if (!res.ok) {
            throw vscode.FileSystemError.Unavailable(uri);
        }

        const buffer = await res.arrayBuffer();
        return new Uint8Array(buffer);
    }

    // -----------------------------------------------------------------------
    // writeFile
    // -----------------------------------------------------------------------

    /**
     * Write content to a file (create or overwrite).
     *
     * After a successful write the parent directory cache is invalidated and
     * an onDidChangeFile event is fired.
     *
     * @throws {vscode.FileSystemError.NoPermissions} when the backend returns 403.
     */
    async writeFile(
        uri: vscode.Uri,
        content: Uint8Array,
        options: { create: boolean; overwrite: boolean }
    ): Promise<void> {
        const { workspaceId, filePath } = this._parseUri(uri);
        const url = `${this._getBaseUrl()}/workspace/${encodeURIComponent(workspaceId)}/files/${this._encodePath(filePath)}/content`;

        let res: Response;
        try {
            res = await fetch(url, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/octet-stream' },
                body: content,
            });
        } catch {
            throw vscode.FileSystemError.Unavailable(uri);
        }

        if (res.status === 403) {
            throw vscode.FileSystemError.NoPermissions(uri);
        }
        if (!res.ok) {
            throw vscode.FileSystemError.Unavailable(uri);
        }

        // Invalidate parent directory cache.
        const parentUri = this._parentUri(uri);
        this._directoryCache.delete(parentUri.toString());

        this._onDidChangeFile.fire([{ type: vscode.FileChangeType.Changed, uri }]);
    }

    // -----------------------------------------------------------------------
    // delete
    // -----------------------------------------------------------------------

    /**
     * Delete a file or directory.
     *
     * @throws {vscode.FileSystemError.FileNotFound} when the backend returns 404.
     * @throws {vscode.FileSystemError.NoPermissions} when the backend returns 403.
     */
    async delete(
        uri: vscode.Uri,
        options: { recursive: boolean }
    ): Promise<void> {
        const { workspaceId, filePath } = this._parseUri(uri);
        const url =
            `${this._getBaseUrl()}/workspace/${encodeURIComponent(workspaceId)}/files/${this._encodePath(filePath)}` +
            `?recursive=${options.recursive}`;

        let res: Response;
        try {
            res = await fetch(url, { method: 'DELETE' });
        } catch {
            throw vscode.FileSystemError.Unavailable(uri);
        }

        if (res.status === 404) {
            throw vscode.FileSystemError.FileNotFound(uri);
        }
        if (res.status === 403) {
            throw vscode.FileSystemError.NoPermissions(uri);
        }
        if (!res.ok) {
            throw vscode.FileSystemError.Unavailable(uri);
        }

        // Invalidate caches.
        this._directoryCache.delete(uri.toString());
        this._directoryCache.delete(this._parentUri(uri).toString());

        this._onDidChangeFile.fire([{ type: vscode.FileChangeType.Deleted, uri }]);
    }

    // -----------------------------------------------------------------------
    // rename
    // -----------------------------------------------------------------------

    /**
     * Rename (move) a file or directory.
     *
     * @throws {vscode.FileSystemError.FileExists} when the target exists and
     *         `options.overwrite` is `false`.
     */
    async rename(
        oldUri: vscode.Uri,
        newUri: vscode.Uri,
        options: { overwrite: boolean }
    ): Promise<void> {
        const { workspaceId, filePath: oldPath } = this._parseUri(oldUri);
        const { filePath: newPath } = this._parseUri(newUri);

        const url = `${this._getBaseUrl()}/workspace/${encodeURIComponent(workspaceId)}/files/${this._encodePath(oldPath)}/rename`;

        let res: Response;
        try {
            res = await fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ new_path: newPath, overwrite: options.overwrite }),
            });
        } catch {
            throw vscode.FileSystemError.Unavailable(oldUri);
        }

        if (res.status === 409) {
            throw vscode.FileSystemError.FileExists(newUri);
        }
        if (!res.ok) {
            throw vscode.FileSystemError.Unavailable(oldUri);
        }

        // Invalidate caches for both old and new locations.
        this._directoryCache.delete(this._parentUri(oldUri).toString());
        this._directoryCache.delete(this._parentUri(newUri).toString());

        this._onDidChangeFile.fire([
            { type: vscode.FileChangeType.Deleted, uri: oldUri },
            { type: vscode.FileChangeType.Created, uri: newUri },
        ]);
    }

    // -----------------------------------------------------------------------
    // createDirectory
    // -----------------------------------------------------------------------

    /**
     * Create a directory.
     *
     * @throws {vscode.FileSystemError.FileExists} when the directory already exists.
     */
    async createDirectory(uri: vscode.Uri): Promise<void> {
        const { workspaceId, filePath } = this._parseUri(uri);
        const url = `${this._getBaseUrl()}/workspace/${encodeURIComponent(workspaceId)}/files/${this._encodePath(filePath)}`;

        let res: Response;
        try {
            res = await fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ type: 'directory' }),
            });
        } catch {
            throw vscode.FileSystemError.Unavailable(uri);
        }

        if (res.status === 409) {
            throw vscode.FileSystemError.FileExists(uri);
        }
        if (!res.ok) {
            throw vscode.FileSystemError.Unavailable(uri);
        }

        // Invalidate parent cache.
        this._directoryCache.delete(this._parentUri(uri).toString());

        this._onDidChangeFile.fire([{ type: vscode.FileChangeType.Created, uri }]);
    }

    // -----------------------------------------------------------------------
    // Cache management (exposed for testing)
    // -----------------------------------------------------------------------

    /** @internal Clears the entire directory cache. Used in tests. */
    _clearCache(): void {
        this._directoryCache.clear();
    }

    /** @internal Returns the number of entries currently in the cache. */
    _cacheSize(): number {
        return this._directoryCache.size;
    }

    // -----------------------------------------------------------------------
    // URI helpers
    // -----------------------------------------------------------------------

    /**
     * Split a conductor:// URI into its workspaceId and file-path components.
     *
     * conductor://my-workspace-id/path/to/file.ts
     *   ⇒ { workspaceId: 'my-workspace-id', filePath: 'path/to/file.ts' }
     */
    private _parseUri(uri: vscode.Uri): { workspaceId: string; filePath: string } {
        const workspaceId = uri.authority;
        // uri.path starts with '/' – strip it.
        const filePath = uri.path.replace(/^\//, '');
        return { workspaceId, filePath };
    }

    /**
     * Encode a file path for use in a URL, encoding each segment individually
     * so that '/' separators are preserved for FastAPI's {path:path} parameter.
     */
    private _encodePath(filePath: string): string {
        return filePath.split('/').map(encodeURIComponent).join('/');
    }

    /**
     * Return the parent URI of the given URI.
     *
     * conductor://ws-id/a/b/c.ts  ⇒  conductor://ws-id/a/b
     */
    private _parentUri(uri: vscode.Uri): vscode.Uri {
        const parts = uri.path.split('/');
        parts.pop();
        return uri.with({ path: parts.join('/') || '/' });
    }
}
