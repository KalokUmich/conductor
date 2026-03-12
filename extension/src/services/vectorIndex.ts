/**
 * In-memory cosine-similarity vector index for Conductor.
 *
 * Stores normalised Float32 vectors and supports brute-force nearest-neighbour
 * search with a target of < 10 ms for 5 000 × 1 024-dim vectors on a modern
 * laptop.
 *
 * No VS Code dependency — fully testable under the Node.js test runner.
 *
 * @module services/vectorIndex
 */

import type { ConductorDb } from './conductorDb';

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

/**
 * A vector row accepted by `loadRows()`.
 *
 * The `vector` field is intentionally flexible so that both the test helper
 * shape `{ buffer, byteOffset }` and the DB shape `Buffer` are accepted.
 */
export interface VectorRow {
    symbol_id: string;
    dim:       number;
    vector:    { buffer: ArrayBufferLike; byteOffset: number } | Buffer;
}

/** A single search result returned by `search()`. */
export interface SearchResult {
    symbol_id: string;
    /** Cosine similarity in the range [-1, 1]. */
    score: number;
}

// ---------------------------------------------------------------------------
// VectorIndex
// ---------------------------------------------------------------------------

export class VectorIndex {
    private _ids:    string[]      = [];
    /** Contiguous flat array: vectors[i * dim .. (i+1) * dim] = i-th normalised vector. */
    private _matrix: Float32Array  = new Float32Array(0);
    private _dim:    number        = 0;
    private _size:   number        = 0;

    /** Number of vectors currently stored in the index. */
    get size(): number { return this._size; }

    /** Dimensionality of stored vectors (0 when the index is empty). */
    get dim(): number { return this._dim; }

    // -----------------------------------------------------------------------
    // Loading
    // -----------------------------------------------------------------------

    /**
     * Replace the current index contents with the provided rows.
     *
     * - Rows with `dim = 0` are ignored.
     * - Rows whose `dim` differs from the first valid row are silently skipped.
     * - Each vector is L2-normalised before storage so `search()` can use a
     *   simple dot product instead of computing norms on every query.
     */
    loadRows(rows: VectorRow[]): void {
        // Filter to valid rows.
        const valid = rows.filter(r => r.dim > 0);

        if (valid.length === 0) {
            this._reset();
            return;
        }

        const dim = valid[0].dim;
        const accepted = valid.filter(r => r.dim === dim);

        this._dim  = dim;
        this._size = accepted.length;
        this._ids  = new Array(accepted.length);
        this._matrix = new Float32Array(accepted.length * dim);

        for (let i = 0; i < accepted.length; i++) {
            const row = accepted[i];
            this._ids[i] = row.symbol_id;

            // Interpret vector bytes as Float32 regardless of the source shape.
            const src: Float32Array = row.vector instanceof Buffer
                ? new Float32Array(row.vector.buffer, row.vector.byteOffset, dim)
                : new Float32Array(
                    (row.vector as { buffer: ArrayBufferLike; byteOffset: number }).buffer,
                    (row.vector as { buffer: ArrayBufferLike; byteOffset: number }).byteOffset,
                    dim,
                  );

            // L2-normalise and store.
            let norm = 0;
            for (let j = 0; j < dim; j++) norm += src[j] * src[j];
            norm = Math.sqrt(norm) || 1;

            const base = i * dim;
            for (let j = 0; j < dim; j++) {
                this._matrix[base + j] = src[j] / norm;
            }
        }
    }

    /**
     * Populate the index from the given `ConductorDb`, loading only vectors
     * produced by `model`.
     */
    load(db: ConductorDb, model: string): void {
        const rows = db.getAllVectorsByModel(model);
        this.loadRows(rows);
    }

    // -----------------------------------------------------------------------
    // Search
    // -----------------------------------------------------------------------

    /**
     * Return the `topK` most similar symbol IDs (by cosine similarity) to `query`.
     *
     * Returns `[]` when the index is empty, `topK` is 0, or the query
     * dimensionality does not match the index dimensionality.
     *
     * The query vector is NOT mutated.
     */
    search(query: Float32Array, topK: number): SearchResult[] {
        if (this._size === 0 || topK === 0) { return []; }
        if (query.length !== this._dim)     { return []; }

        // Normalise the query vector (working copy to avoid mutation).
        const q = new Float32Array(this._dim);
        let qNorm = 0;
        for (let j = 0; j < this._dim; j++) qNorm += query[j] * query[j];
        qNorm = Math.sqrt(qNorm) || 1;
        for (let j = 0; j < this._dim; j++) q[j] = query[j] / qNorm;

        // Brute-force dot-product over pre-normalised matrix rows.
        const scores = new Float32Array(this._size);
        const dim    = this._dim;
        const matrix = this._matrix;
        for (let i = 0; i < this._size; i++) {
            let dot = 0;
            const base = i * dim;
            for (let j = 0; j < dim; j++) dot += matrix[base + j] * q[j];
            scores[i] = dot;
        }

        // Partial sort: pick the top-K indices by descending score.
        const k = Math.min(topK, this._size);
        const indices = Array.from({ length: this._size }, (_, i) => i);
        indices.sort((a, b) => scores[b] - scores[a]);

        return indices.slice(0, k).map(i => ({
            symbol_id: this._ids[i],
            score:     scores[i],
        }));
    }

    // -----------------------------------------------------------------------
    // Private helpers
    // -----------------------------------------------------------------------

    private _reset(): void {
        this._ids    = [];
        this._matrix = new Float32Array(0);
        this._dim    = 0;
        this._size   = 0;
    }
}

