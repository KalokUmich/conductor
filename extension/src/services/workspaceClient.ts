/**
 * WorkspaceClient – HTTP client for the /workspace/ backend API.
 *
 * Provides typed wrappers around the REST endpoints exposed by the Conductor
 * backend service.  All network I/O is performed with the native `fetch` API
 * (available in Node 18+ and VS Code’s extension host).
 *
 * The client is intentionally free of VS Code API dependencies so that it can
 * be unit-tested in a plain Node environment.
 *
 * @module services/workspaceClient
 */

// ---------------------------------------------------------------------------
// DTOs (Data-Transfer Objects)
// ---------------------------------------------------------------------------

/**
 * Payload sent to POST /workspace/
 */
export interface CreateWorkspaceRequest {
    /** Human-readable workspace name. */
    name: string;
    /** Template identifier (e.g. "python-3.11", "node-20"). */
    template: string;
    /** Optional Git repository URL to clone into the workspace. */
    git_repo_url?: string;
}

/**
 * Response body from POST /workspace/ and GET /workspace/{id}
 */
export interface WorkspaceInfo {
    /** Unique workspace identifier. */
    id: string;
    /** Human-readable workspace name. */
    name: string;
    /** Template used to create the workspace. */
    template: string;
    /** Current lifecycle status. */
    status: 'creating' | 'ready' | 'stopping' | 'stopped' | 'error';
    /** ISO-8601 creation timestamp. */
    created_at: string;
    /** ISO-8601 last-updated timestamp. */
    updated_at: string;
    /** SSH connection string, present once the workspace is ready. */
    ssh_connection_string?: string;
    /** Web IDE URL, present once the workspace is ready. */
    web_ide_url?: string;
}

/**
 * Response body from GET /workspace/
 */
export interface WorkspaceListResponse {
    items: WorkspaceInfo[];
    total: number;
    page: number;
    page_size: number;
}

// ---------------------------------------------------------------------------
// Error type
// ---------------------------------------------------------------------------

/**
 * Thrown when the backend returns a non-2xx HTTP response.
 */
export class WorkspaceApiError extends Error {
    constructor(
        public readonly status: number,
        public readonly body: string,
        message?: string
    ) {
        super(message ?? `HTTP ${status}: ${body}`);
        this.name = 'WorkspaceApiError';
    }
}

// ---------------------------------------------------------------------------
// Client
// ---------------------------------------------------------------------------

/**
 * Thin HTTP client that wraps the Conductor backend /workspace/ API.
 *
 * ```ts
 * const client = new WorkspaceClient('http://localhost:8000');
 * const info   = await client.createWorkspace({ name: 'my-ws', template: 'python-3.11' });
 * ```
 */
export class WorkspaceClient {
    private readonly _base: string;

    /**
     * @param baseUrl  Base URL of the backend service, e.g. "http://localhost:8000".
     *                 A trailing slash is tolerated.
     */
    constructor(baseUrl: string) {
        this._base = baseUrl.replace(/\/$/, '');
    }

    // -----------------------------------------------------------------------
    // Health
    // -----------------------------------------------------------------------

    /**
     * Returns `true` when the backend /health endpoint responds with 200.
     * Never throws – network errors are swallowed and return `false`.
     */
    async isBackendAlive(): Promise<boolean> {
        try {
            const res = await fetch(`${this._base}/health`);
            return res.ok;
        } catch {
            return false;
        }
    }

    // -----------------------------------------------------------------------
    // CRUD
    // -----------------------------------------------------------------------

    /**
     * Create a new workspace.
     *
     * @throws {WorkspaceApiError} on non-2xx response.
     */
    async createWorkspace(req: CreateWorkspaceRequest): Promise<WorkspaceInfo> {
        const res = await fetch(`${this._base}/workspace/`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(req),
        });
        return this._parseResponse<WorkspaceInfo>(res);
    }

    /**
     * Retrieve a single workspace by ID.
     *
     * @throws {WorkspaceApiError} on non-2xx response.
     */
    async getWorkspace(id: string): Promise<WorkspaceInfo> {
        const res = await fetch(`${this._base}/workspace/${encodeURIComponent(id)}`);
        return this._parseResponse<WorkspaceInfo>(res);
    }

    /**
     * List all workspaces, with optional pagination.
     *
     * @throws {WorkspaceApiError} on non-2xx response.
     */
    async listWorkspaces(page = 1, pageSize = 20): Promise<WorkspaceListResponse> {
        const url = `${this._base}/workspace/?page=${page}&page_size=${pageSize}`;
        const res = await fetch(url);
        return this._parseResponse<WorkspaceListResponse>(res);
    }

    /**
     * Update workspace metadata (name only for now).
     *
     * @throws {WorkspaceApiError} on non-2xx response.
     */
    async updateWorkspace(
        id: string,
        patch: Partial<Pick<WorkspaceInfo, 'name'>>
    ): Promise<WorkspaceInfo> {
        const res = await fetch(
            `${this._base}/workspace/${encodeURIComponent(id)}`,
            {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(patch),
            }
        );
        return this._parseResponse<WorkspaceInfo>(res);
    }

    /**
     * Delete a workspace by ID.
     *
     * @throws {WorkspaceApiError} on non-2xx response.
     */
    async deleteWorkspace(id: string): Promise<void> {
        const res = await fetch(
            `${this._base}/workspace/${encodeURIComponent(id)}`,
            { method: 'DELETE' }
        );
        if (!res.ok) {
            const body = await res.text();
            throw new WorkspaceApiError(res.status, body);
        }
    }

    /**
     * Poll a workspace until its status is `ready` or `error`.
     *
     * @param id              Workspace ID.
     * @param intervalMs      Polling interval in milliseconds (default 2000).
     * @param timeoutMs       Maximum wait time in milliseconds (default 120000).
     * @throws {Error}        When the timeout is reached before a terminal state.
     * @throws {WorkspaceApiError} on non-2xx response during polling.
     */
    async pollUntilReady(
        id: string,
        intervalMs = 2_000,
        timeoutMs = 120_000
    ): Promise<WorkspaceInfo> {
        const deadline = Date.now() + timeoutMs;
        while (Date.now() < deadline) {
            const info = await this.getWorkspace(id);
            if (info.status === 'ready' || info.status === 'error') {
                return info;
            }
            await new Promise<void>((resolve) => setTimeout(resolve, intervalMs));
        }
        throw new Error(`Workspace ${id} did not become ready within ${timeoutMs} ms`);
    }

    // -----------------------------------------------------------------------
    // Private helpers
    // -----------------------------------------------------------------------

    private async _parseResponse<T>(res: Response): Promise<T> {
        const text = await res.text();
        if (!res.ok) {
            throw new WorkspaceApiError(res.status, text);
        }
        try {
            return JSON.parse(text) as T;
        } catch {
            throw new WorkspaceApiError(res.status, text, `Failed to parse JSON: ${text}`);
        }
    }
}
