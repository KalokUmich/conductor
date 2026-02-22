/**
 * @deprecated Use backend RAG (ragClient.ts) instead. Embedding is now handled
 * server-side by the FAISS-based RAG pipeline (2.2).
 *
 * HTTP client for the backend POST /embeddings endpoint.
 *
 * The extension MUST NOT call cloud providers (Bedrock, OpenAI, …) directly.
 * All embedding generation is delegated to the Conductor backend, which
 * handles credentials, rate-limiting, and provider abstraction.
 *
 * No VS Code dependency — fully testable under the Node.js test runner.
 *
 * @module services/embeddingClient
 */

const LOG = '[EmbeddingClient]';

/** Raw response body from POST /embeddings. */
interface EmbedResponseBody {
    vectors: number[][];
    model: string;
    dim: number;
}

export class EmbeddingClient {
    /**
     * @param backendUrl  Base URL of the Conductor backend, e.g. `http://127.0.0.1:8000`.
     *                    No trailing slash.
     */
    constructor(private readonly backendUrl: string) {}

    /**
     * Send a batch of texts to the backend and receive embedding vectors.
     *
     * @param texts  1–32 strings to embed.
     * @returns      One `number[]` vector per input text, in the same order.
     * @throws       On non-2xx HTTP responses or network errors.
     */
    async embed(texts: string[]): Promise<number[][]> {
        const url = `${this.backendUrl}/embeddings`;
        console.log(`${LOG} POST ${url} texts=${texts.length}`);

        let response: Response;
        try {
            response = await fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ texts }),
            });
        } catch (err) {
            throw new Error(`${LOG} Network error calling POST /embeddings: ${err}`);
        }

        if (!response.ok) {
            const body = await response.text().catch(() => '');
            throw new Error(
                `${LOG} POST /embeddings failed: HTTP ${response.status} ${body}`,
            );
        }

        const data = (await response.json()) as EmbedResponseBody;

        if (!Array.isArray(data.vectors)) {
            throw new Error(
                `${LOG} Unexpected response shape — 'vectors' field missing or not an array`,
            );
        }
        if (data.vectors.length !== texts.length) {
            throw new Error(
                `${LOG} Backend returned ${data.vectors.length} vectors for ${texts.length} texts`,
            );
        }

        console.log(
            `${LOG} received ${data.vectors.length} vector(s) model=${data.model} dim=${data.dim}`,
        );
        return data.vectors;
    }

    /**
     * Convert a raw number array (from the backend JSON) to a `Float32Array`
     * suitable for storage in SQLite as a BLOB.
     *
     * @param vector  Array of float values returned by `embed()`.
     * @returns       A `Float32Array` sharing no memory with `vector`.
     */
    toFloat32Array(vector: number[]): Float32Array {
        return new Float32Array(vector);
    }
}
