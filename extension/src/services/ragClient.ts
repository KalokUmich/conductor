/**
 * HTTP client for the Conductor backend RAG (Retrieval-Augmented Generation) API.
 *
 * Exposes three endpoints:
 *   - `index()`   — initial full workspace indexing
 *   - `reindex()` — incremental re-indexing of changed files
 *   - `search()`  — semantic code search
 *
 * @module services/ragClient
 */

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

/** An individual file change to send to the backend. */
export interface RagFileChange {
    path:    string;
    content: string;
    action:  'upsert' | 'delete';
}

/** Response from index / reindex operations. */
export interface RagIndexResponse {
    chunks_added:    number;
    chunks_removed:  number;
    files_processed: number;
}

/** A single result from a semantic search. */
export interface RagSearchResult {
    file_path:   string;
    start_line:  number;
    end_line:    number;
    symbol_name: string;
    symbol_type: string;
    content:     string;
    score:       number;
    language:    string;
}

/** Response from search operation. */
export interface RagSearchResponse {
    results:      RagSearchResult[];
    query:        string;
    workspace_id: string;
}

/** Optional filters for search. */
export interface RagSearchFilters {
    languages?:     string[];
    file_patterns?: string[];
}

// ---------------------------------------------------------------------------
// RagClient
// ---------------------------------------------------------------------------

export class RagClient {
    private readonly _baseUrl: string;
    private _abortController: AbortController = new AbortController();

    constructor(baseUrl: string) {
        // Strip trailing slash for consistent URL construction.
        this._baseUrl = baseUrl.replace(/\/+$/, '');
    }

    /**
     * Abort any in-flight requests and prevent new ones from starting.
     *
     * Once cancelled, the `RagClient` instance should be discarded.
     */
    cancel(): void {
        this._abortController.abort();
    }

    /**
     * Index a set of files (initial full workspace indexing).
     */
    async index(workspaceId: string, files: RagFileChange[]): Promise<RagIndexResponse> {
        return this._post<RagIndexResponse>('/rag/index', { workspace_id: workspaceId, files });
    }

    /**
     * Re-index a set of changed files (incremental update).
     */
    async reindex(workspaceId: string, files: RagFileChange[]): Promise<RagIndexResponse> {
        return this._post<RagIndexResponse>('/rag/reindex', { workspace_id: workspaceId, files });
    }

    /**
     * Perform a semantic code search.
     *
     * @param workspaceId - Workspace to search within.
     * @param query       - Natural-language search query.
     * @param topK        - Optional maximum number of results.
     * @param filters     - Optional language / file-pattern filters.
     */
    async search(
        workspaceId: string,
        query:       string,
        topK?:       number,
        filters?:    RagSearchFilters,
    ): Promise<RagSearchResponse> {
        const body: Record<string, unknown> = { workspace_id: workspaceId, query };
        if (topK    !== undefined) { body.top_k   = topK;    }
        if (filters !== undefined) { body.filters = filters; }
        return this._post<RagSearchResponse>('/rag/search', body);
    }

    // -----------------------------------------------------------------------
    // Private helpers
    // -----------------------------------------------------------------------

    private async _post<T>(path: string, body: unknown): Promise<T> {
        const url = `${this._baseUrl}${path}`;

        let response: Response;
        try {
            response = await fetch(url, {
                method:  'POST',
                headers: { 'Content-Type': 'application/json' },
                body:    JSON.stringify(body),
                signal:  this._abortController.signal,
            });
        } catch (err: unknown) {
            const msg = err instanceof Error ? err.message : String(err);
            throw new Error(`Network error calling ${url}: ${msg}`);
        }

        if (!response.ok) {
            throw new Error(
                `RAG request failed with status ${response.status}: ${url}`,
            );
        }

        return response.json() as Promise<T>;
    }
}

