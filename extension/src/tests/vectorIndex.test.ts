/**
 * Tests for VectorIndex — local semantic search engine.
 *
 * Uses synthetic in-memory rows (via VectorIndex.loadRows) so no DB writes
 * are needed for most tests.  A small subset uses a real ConductorDb in
 * a tmpdir to verify the DB integration path.
 *
 * Run after compilation:
 *   node --test out/tests/vectorIndex.test.js
 */
import { describe, it, beforeEach, afterEach } from 'node:test';
import * as assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as os from 'node:os';

import { VectorIndex, VectorRow, SearchResult } from '../services/vectorIndex';
import { ConductorDb } from '../services/conductorDb';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Build a VectorRow from a plain number array (no DB needed). */
function makeRow(id: string, values: number[]): VectorRow {
    const f32 = new Float32Array(values);
    return {
        symbol_id: id,
        dim: values.length,
        vector: { buffer: f32.buffer as ArrayBufferLike, byteOffset: f32.byteOffset },
    };
}

/** Build a SymbolVectorRow-compatible object for DB insertion. */
function makeDbRow(id: string, values: number[], model = 'test-model') {
    const f32 = new Float32Array(values);
    return {
        symbol_id: id,
        dim: values.length,
        vector: Buffer.from(f32.buffer),
        model,
        sha1: 'sha-' + id,
    };
}

/** L2-norm of a float array (for assertion helpers). */
function norm(v: number[]): number {
    return Math.sqrt(v.reduce((s, x) => s + x * x, 0));
}

/** Dot product of two equal-length arrays. */
function dot(a: number[], b: number[]): number {
    let s = 0;
    for (let i = 0; i < a.length; i++) s += a[i] * b[i];
    return s;
}

/** Expected cosine similarity between two unnormalised vectors. */
function cosine(a: number[], b: number[]): number {
    return dot(a, b) / (norm(a) * norm(b));
}

// ---------------------------------------------------------------------------
// Empty index
// ---------------------------------------------------------------------------

describe('VectorIndex — empty index', () => {
    it('size is 0 on construction', () => {
        assert.equal(new VectorIndex().size, 0);
    });

    it('dim is 0 on construction', () => {
        assert.equal(new VectorIndex().dim, 0);
    });

    it('search returns [] on empty index', () => {
        assert.deepEqual(new VectorIndex().search(new Float32Array([1, 0]), 5), []);
    });

    it('search returns [] when topK = 0', () => {
        const idx = new VectorIndex();
        idx.loadRows([makeRow('s', [1, 0])]);
        assert.deepEqual(idx.search(new Float32Array([1, 0]), 0), []);
    });

    it('loadRows with empty array resets the index', () => {
        const idx = new VectorIndex();
        idx.loadRows([makeRow('s', [1, 2, 3])]);
        idx.loadRows([]);
        assert.equal(idx.size, 0);
        assert.equal(idx.dim, 0);
    });

    it('loadRows with dim=0 row resets the index', () => {
        const idx = new VectorIndex();
        idx.loadRows([{ symbol_id: 's', dim: 0, vector: { buffer: new ArrayBuffer(0), byteOffset: 0 } }]);
        assert.equal(idx.size, 0);
    });
});

// ---------------------------------------------------------------------------
// loadRows — basic properties
// ---------------------------------------------------------------------------

describe('VectorIndex — loadRows', () => {
    it('sets size and dim correctly', () => {
        const idx = new VectorIndex();
        idx.loadRows([makeRow('a', [1, 2, 3]), makeRow('b', [4, 5, 6])]);
        assert.equal(idx.size, 2);
        assert.equal(idx.dim, 3);
    });

    it('skips rows whose dim differs from the first row', () => {
        const idx = new VectorIndex();
        idx.loadRows([
            makeRow('a', [1, 2]),
            makeRow('b', [1, 2, 3]), // dim mismatch — should be skipped
        ]);
        assert.equal(idx.size, 1);
    });

    it('reload replaces the previous index', () => {
        const idx = new VectorIndex();
        idx.loadRows([makeRow('old', [1, 0, 0])]);
        idx.loadRows([makeRow('new1', [0, 1, 0]), makeRow('new2', [0, 0, 1])]);
        assert.equal(idx.size, 2);
        assert.equal(idx.dim, 3);
        const r = idx.search(new Float32Array([0, 1, 0]), 1);
        assert.equal(r[0].symbol_id, 'new1');
    });

    it('accepts Buffer-backed rows (SymbolVectorRow shape)', () => {
        const idx = new VectorIndex();
        const f32 = new Float32Array([1, 0, 0]);
        const row = {
            symbol_id: 'buf',
            dim: 3,
            vector: Buffer.from(f32.buffer),
        };
        idx.loadRows([row]);
        assert.equal(idx.size, 1);
    });
});

// ---------------------------------------------------------------------------
// Cosine similarity correctness
// ---------------------------------------------------------------------------

describe('VectorIndex — cosine similarity correctness', () => {
    it('identical vectors score ≈ 1.0', () => {
        const v = [1, 2, 3, 4];
        const idx = new VectorIndex();
        idx.loadRows([makeRow('s', v)]);
        const [r] = idx.search(new Float32Array(v), 1);
        assert.ok(
            Math.abs(r.score - 1.0) < 1e-5,
            `Expected ~1.0, got ${r.score}`,
        );
    });

    it('opposite vectors score ≈ -1.0', () => {
        const v = [1, 0, 0];
        const idx = new VectorIndex();
        idx.loadRows([makeRow('s', v)]);
        const [r] = idx.search(new Float32Array([-1, 0, 0]), 1);
        assert.ok(
            Math.abs(r.score - (-1.0)) < 1e-5,
            `Expected ≈ -1.0, got ${r.score}`,
        );
    });

    it('orthogonal vectors score ≈ 0.0', () => {
        const idx = new VectorIndex();
        idx.loadRows([makeRow('s', [1, 0, 0])]);
        const [r] = idx.search(new Float32Array([0, 1, 0]), 1);
        assert.ok(Math.abs(r.score) < 1e-5, `Expected ≈ 0.0, got ${r.score}`);
    });

    it('score matches manual cosine calculation', () => {
        const a = [3, 1, 4, 1, 5, 9, 2, 6];
        const b = [2, 7, 1, 8, 2, 8, 1, 8];
        const expected = cosine(a, b);

        const idx = new VectorIndex();
        idx.loadRows([makeRow('s', a)]);
        const [r] = idx.search(new Float32Array(b), 1);
        assert.ok(
            Math.abs(r.score - expected) < 1e-5,
            `Expected ${expected}, got ${r.score}`,
        );
    });

    it('score is in [-1, 1]', () => {
        const idx = new VectorIndex();
        for (let i = 0; i < 20; i++) {
            idx.loadRows([makeRow(`s${i}`, [Math.random() - 0.5, Math.random() - 0.5, Math.random() - 0.5])]);
        }
        idx.loadRows(
            Array.from({ length: 20 }, (_, i) =>
                makeRow(`s${i}`, [Math.random() - 0.5, Math.random() - 0.5, Math.random() - 0.5]),
            ),
        );
        const results = idx.search(new Float32Array([1, 1, 1]), 20);
        for (const r of results) {
            assert.ok(r.score >= -1.001 && r.score <= 1.001, `Score ${r.score} out of range`);
        }
    });

    it('query vector is not mutated by search', () => {
        const idx = new VectorIndex();
        idx.loadRows([makeRow('s', [1, 2, 3])]);
        const q = new Float32Array([4, 5, 6]);
        const before = Array.from(q);
        idx.search(q, 1);
        assert.deepEqual(Array.from(q), before);
    });
});

// ---------------------------------------------------------------------------
// Ranking order
// ---------------------------------------------------------------------------

describe('VectorIndex — ranking', () => {
    it('most similar symbol is ranked first', () => {
        const idx = new VectorIndex();
        // s1 = [1,0], s2 = [0,1]; query [1,0] should rank s1 first
        idx.loadRows([makeRow('s1', [1, 0]), makeRow('s2', [0, 1])]);
        const r = idx.search(new Float32Array([1, 0]), 2);
        assert.equal(r[0].symbol_id, 's1');
        assert.equal(r[1].symbol_id, 's2');
    });

    it('results are ordered by descending score', () => {
        const idx = new VectorIndex();
        idx.loadRows([
            makeRow('a', [1, 0, 0]),
            makeRow('b', [0.7, 0.7, 0]),
            makeRow('c', [0, 0, 1]),
        ]);
        const results = idx.search(new Float32Array([1, 0, 0]), 3);
        for (let i = 1; i < results.length; i++) {
            assert.ok(
                results[i - 1].score >= results[i].score,
                `scores out of order at [${i - 1}]=${results[i - 1].score} [${i}]=${results[i].score}`,
            );
        }
    });

    it('topK limits the number of results', () => {
        const idx = new VectorIndex();
        idx.loadRows(Array.from({ length: 10 }, (_, i) => makeRow(`s${i}`, [i, 1])));
        assert.equal(idx.search(new Float32Array([1, 0]), 3).length, 3);
    });

    it('topK > size returns all results', () => {
        const idx = new VectorIndex();
        idx.loadRows([makeRow('a', [1, 0]), makeRow('b', [0, 1])]);
        assert.equal(idx.search(new Float32Array([1, 0]), 100).length, 2);
    });

    it('topK = 1 returns exactly one result', () => {
        const idx = new VectorIndex();
        idx.loadRows([makeRow('a', [1, 0]), makeRow('b', [0, 1]), makeRow('c', [1, 1])]);
        assert.equal(idx.search(new Float32Array([1, 0]), 1).length, 1);
    });
});

// ---------------------------------------------------------------------------
// Dim mismatch guard
// ---------------------------------------------------------------------------

describe('VectorIndex — dim mismatch', () => {
    it('returns [] when query dim !== index dim', () => {
        const idx = new VectorIndex();
        idx.loadRows([makeRow('s', [1, 2, 3])]);
        const result = idx.search(new Float32Array([1, 2]), 5); // dim 2, index is dim 3
        assert.deepEqual(result, []);
    });

    it('returns [] when query dim > index dim', () => {
        const idx = new VectorIndex();
        idx.loadRows([makeRow('s', [1, 0])]);
        const result = idx.search(new Float32Array([1, 0, 0, 0]), 5);
        assert.deepEqual(result, []);
    });
});

// ---------------------------------------------------------------------------
// DB integration
// ---------------------------------------------------------------------------

describe('VectorIndex — DB integration', () => {
    let tmpDir: string;
    let db: ConductorDb;

    beforeEach(() => {
        tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'vector-idx-test-'));
        db = new ConductorDb(path.join(tmpDir, 'cache.db'));
    });

    afterEach(() => {
        try { db.close(); } catch { /* ok */ }
        fs.rmSync(tmpDir, { recursive: true, force: true });
    });

    it('load() populates index from DB with matching model', () => {
        db.upsertSymbolVector(makeDbRow('s1', [1, 0, 0], 'model-a'));
        db.upsertSymbolVector(makeDbRow('s2', [0, 1, 0], 'model-a'));
        db.upsertSymbolVector(makeDbRow('s3', [0, 0, 1], 'model-b')); // different model

        const idx = new VectorIndex();
        idx.load(db, 'model-a');
        assert.equal(idx.size, 2);
    });

    it('load() excludes vectors for other models', () => {
        db.upsertSymbolVector(makeDbRow('s1', [1, 0], 'model-x'));
        db.upsertSymbolVector(makeDbRow('s2', [0, 1], 'model-y'));

        const idx = new VectorIndex();
        idx.load(db, 'model-x');
        assert.equal(idx.size, 1);
        const [r] = idx.search(new Float32Array([1, 0]), 5);
        assert.equal(r.symbol_id, 's1');
    });

    it('load() on empty DB gives empty index', () => {
        const idx = new VectorIndex();
        idx.load(db, 'any-model');
        assert.equal(idx.size, 0);
    });

    it('search result symbol_ids match what was stored in DB', () => {
        db.upsertSymbolVector(makeDbRow('greet', [1, 0, 0]));
        db.upsertSymbolVector(makeDbRow('farewell', [0, 1, 0]));
        db.upsertSymbolVector(makeDbRow('compute', [0, 0, 1]));

        const idx = new VectorIndex();
        idx.load(db, 'test-model');
        const results = idx.search(new Float32Array([1, 0, 0]), 3);
        assert.ok(results.some(r => r.symbol_id === 'greet'));
        assert.equal(results[0].symbol_id, 'greet'); // closest to [1,0,0]
    });
});

// ---------------------------------------------------------------------------
// Performance: 5 000 vectors × dim 1 024  (<10 ms target for search())
// ---------------------------------------------------------------------------

describe('VectorIndex — performance', () => {
    it('search over 5 000 × 1 024-dim vectors completes in <10 ms', () => {
        const N   = 5_000;
        const DIM = 1_024;

        // Build synthetic rows in memory (no DB writes needed).
        const rows: VectorRow[] = new Array(N);
        for (let i = 0; i < N; i++) {
            const f32 = new Float32Array(DIM);
            // Fill with deterministic pseudo-random values.
            for (let j = 0; j < DIM; j++) {
                // Simple LCG: different values per (i, j) pair
                f32[j] = ((Math.sin(i * 1000 + j) + 1) * 0.5) - 0.25;
            }
            rows[i] = { symbol_id: `sym${i}`, dim: DIM, vector: { buffer: f32.buffer, byteOffset: 0 } };
        }

        const idx = new VectorIndex();
        idx.loadRows(rows);
        assert.equal(idx.size, N);

        // Query vector: also pseudo-random.
        const query = new Float32Array(DIM);
        for (let j = 0; j < DIM; j++) query[j] = Math.cos(j * 0.01);

        // Warm-up call to allow V8 JIT to compile the hot path.
        idx.search(query, 10);

        // Measure search time.
        const t0 = performance.now();
        const results = idx.search(query, 10);
        const elapsed = performance.now() - t0;

        assert.equal(results.length, 10);
        assert.ok(
            elapsed < 10,
            `search() took ${elapsed.toFixed(2)} ms — expected <10 ms`,
        );
    });
});
