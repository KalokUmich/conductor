/**
 * RagClient — communicates with the backend RAG endpoints for codebase
 * indexing and semantic search.
 *
 * All HTTP calls go through the extension host (not the WebView) to
 * satisfy VS Code's CSP restrictions.
 */

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

export interface RagFileChange {
    path: string;
    content?: string;
    action: 'upsert' | 'delete';
}

export interface RagIndexResponse {
    chunks_added: number;
    chunks_removed: number;
    files_processed: number;
}

export interface RagSearchFilters {
    languages?: string[];
    file_patterns?: string[];
}

export interface RagSearchResultItem {
    file_path: string;
    start_line: number;
    end_line: number;
    symbol_name: string;
    symbol_type: string;
    content: string;
    score: number;
    language: string;
}

export interface RagSearchResponse {
    results: RagSearchResultItem[];
    query: string;
    workspace_id: string;
}

// ---------------------------------------------------------------------------
// RagClient
// ---------------------------------------------------------------------------

export class RagClient {
    private readonly _baseUrl: string;

    constructor(backendUrl: string) {
        // Normalise: strip trailing slash
        this._baseUrl = backendUrl.replace(/\/+$/, '');
    }

    /**
     * Incrementally index (upsert/delete) files.
     */
    async index(workspaceId: string, files: RagFileChange[]): Promise<RagIndexResponse> {
        return this._post<RagIndexResponse>('/rag/index', {
            workspace_id: workspaceId,
            files,
        });
    }

    /**
     * Full reindex: clear existing index and rebuild from provided files.
     */
    async reindex(workspaceId: string, files: RagFileChange[]): Promise<RagIndexResponse> {
        return this._post<RagIndexResponse>('/rag/reindex', {
            workspace_id: workspaceId,
            files,
        });
    }

    /**
     * Semantic search over the indexed codebase.
     */
    async search(
        workspaceId: string,
        query: string,
        topK?: number,
        filters?: RagSearchFilters,
    ): Promise<RagSearchResponse> {
        const body: Record<string, unknown> = {
            workspace_id: workspaceId,
            query,
        };
        if (topK !== undefined) body.top_k = topK;
        if (filters) body.filters = filters;

        return this._post<RagSearchResponse>('/rag/search', body);
    }

    // -----------------------------------------------------------------------
    // Internal
    // -----------------------------------------------------------------------

    private async _post<T>(path: string, body: unknown): Promise<T> {
        const url = `${this._baseUrl}${path}`;
        let response: Response;
        try {
            response = await fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
        } catch (err) {
            throw new Error(`Network error calling ${path}: ${err instanceof Error ? err.message : err}`);
        }

        if (!response.ok) {
            const text = await response.text().catch(() => '');
            throw new Error(`RAG ${path} failed (${response.status}): ${text}`);
        }

        return response.json() as Promise<T>;
    }
}
