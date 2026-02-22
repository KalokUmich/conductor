/**
 * @deprecated Use backend RAG (ragClient.ts) instead. Embedding is now handled
 * server-side by the FAISS-based RAG pipeline (2.2).
 *
 * Background embedding queue for Conductor V1.
 *
 * Manages asynchronous, non-blocking embedding of code symbols using a
 * FIFO work queue with bounded concurrency and single-retry logic.
 *
 * Design constraints
 * ------------------
 * - Max 5 concurrent embedding requests (MAX_CONCURRENCY).
 * - FIFO ordering: jobs are processed in enqueue order within each priority.
 * - Retry once on failure: if the backend call fails, it is retried exactly
 *   once before the job is abandoned and `onError` is called.
 * - Non-blocking: `enqueue()` returns immediately; the caller must NOT await
 *   the embedding result before returning to the user.
 * - Deduplication: jobs whose items all have current sha1+model vectors in
 *   the DB are silently dropped before entering the queue.
 *
 * V1 scope
 * --------
 * Embeds only the symbols explicitly provided by the caller:
 * - The current symbol under the cursor
 * - Symbols from the definition file (via lspResolver)
 * - Symbols from reference files (via lspResolver)
 *
 * No VS Code dependency — fully testable under the Node.js test runner.
 *
 * @module services/embeddingQueue
 */

import { ConductorDb, SymbolVectorRow } from './conductorDb';
import { EmbeddingClient } from './embeddingClient';

const LOG = '[EmbeddingQueue]';

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

/** A single symbol to be embedded. */
export interface EmbeddingJobItem {
    /** Stable unique identifier for the symbol (e.g. `"src/app.ts::greet"`). */
    symbolId: string;
    /** Text to embed — typically the symbol's signature or a short snippet. */
    text: string;
    /**
     * SHA-1 of `text` used for cache invalidation.  If `text` hasn't changed
     * since the last embedding, the stored vector can be reused.
     */
    sha1: string;
}

/** A batch of symbols to embed in one backend call. */
export interface EmbeddingJob {
    /** Items to embed.  Must be non-empty after deduplication. */
    items: EmbeddingJobItem[];
    /** Embedding model ID expected by the backend (e.g. `"cohere.embed-v4"`). */
    model: string;
    /** Expected vector dimensionality (used when persisting). */
    dim: number;
    /** Called with the number of vectors stored after a successful job. */
    onComplete?: (storedCount: number) => void;
    /** Called with the error after a job fails both attempts. */
    onError?: (err: Error) => void;
}

// ---------------------------------------------------------------------------
// Queue
// ---------------------------------------------------------------------------

export class EmbeddingQueue {
    /** Maximum number of jobs that may execute concurrently. */
    static readonly MAX_CONCURRENCY = 5;

    private readonly _queue: EmbeddingJob[] = [];
    private _running = 0;
    private _cancelled = false;

    /**
     * @param _client  HTTP client for the backend /embeddings endpoint.
     * @param _db      Local SQLite store for persisting vectors.
     */
    constructor(
        private readonly _client: EmbeddingClient,
        private readonly _db: ConductorDb,
    ) {}

    // -----------------------------------------------------------------------
    // Public API
    // -----------------------------------------------------------------------

    /**
     * Enqueue a batch of symbols for embedding.
     *
     * Items whose `sha1` and `model` are already current in the DB are
     * filtered out before the job enters the queue.  If all items are
     * already up-to-date, the job is silently dropped and nothing is
     * scheduled.
     *
     * Returns immediately — embedding happens in the background.
     */
    enqueue(job: EmbeddingJob): void {
        if (this._cancelled) return;

        const needed = job.items.filter(
            item => this._db.needsEmbedding(item.symbolId, item.sha1, job.model),
        );

        if (needed.length === 0) {
            console.log(`${LOG} all ${job.items.length} item(s) already up-to-date, skipping`);
            return;
        }

        console.log(
            `${LOG} enqueueing ${needed.length} item(s) (${job.items.length - needed.length} skipped) model=${job.model}`,
        );

        this._queue.push({ ...job, items: needed });
        this._drain();
    }

    /**
     * Cancel this queue: clear all pending jobs and prevent new ones from
     * starting.  Any job already in-flight completes but its results are
     * discarded.
     */
    cancel(): void {
        this._cancelled = true;
        this._queue.length = 0;
    }

    /** Number of jobs waiting in the queue (not yet started). */
    get queueLength(): number {
        return this._queue.length;
    }

    /** Number of jobs currently executing. */
    get runningCount(): number {
        return this._running;
    }

    // -----------------------------------------------------------------------
    // Internal machinery
    // -----------------------------------------------------------------------

    /** Launch up to MAX_CONCURRENCY workers from the front of the queue. */
    private _drain(): void {
        while (
            this._running < EmbeddingQueue.MAX_CONCURRENCY &&
            this._queue.length > 0
        ) {
            const job = this._queue.shift()!;
            this._running++;
            this._runJob(job, 0).finally(() => {
                this._running--;
                this._drain(); // fill the freed slot with the next queued job
            });
        }
    }

    /**
     * Execute a single job, retrying once on failure.
     *
     * @param job      The job to run.
     * @param attempt  0 on first try, 1 on retry.
     */
    private async _runJob(job: EmbeddingJob, attempt: number): Promise<void> {
        if (this._cancelled) return;
        try {
            const texts = job.items.map(i => i.text);
            const vectors = await this._client.embed(texts);

            let stored = 0;
            for (let i = 0; i < job.items.length; i++) {
                const item = job.items[i];
                const vec  = vectors[i];
                if (!vec) continue;

                const f32: Float32Array = new Float32Array(vec);
                const row: SymbolVectorRow = {
                    symbol_id: item.symbolId,
                    dim:       f32.length,
                    vector:    Buffer.from(f32.buffer),
                    model:     job.model,
                    sha1:      item.sha1,
                };
                this._db.upsertSymbolVector(row);
                stored++;
            }

            console.log(
                `${LOG} stored ${stored} vector(s) model=${job.model} attempt=${attempt}`,
            );
            job.onComplete?.(stored);

        } catch (err) {
            const errMsg = err instanceof Error ? err.message : String(err);

            // Non-retryable: the backend disabled the embedding service (expired
            // credentials, invalid token, etc.).  Cancel the entire queue so we
            // don't keep firing API calls that will all 503 until restart.
            if (errMsg.includes('HTTP 503')) {
                console.warn(`${LOG} Embedding service disabled (503) — cancelling queue permanently`);
                this.cancel();
                job.onError?.(new Error(`Embedding service unavailable: ${errMsg}`));
                return;
            }

            if (attempt === 0) {
                console.log(`${LOG} attempt 1 failed, retrying: ${err}`);
                return this._runJob(job, 1);
            }
            const error = err instanceof Error ? err : new Error(String(err));
            console.log(`${LOG} job failed after retry: ${error.message}`);
            job.onError?.(error);
        }
    }
}
