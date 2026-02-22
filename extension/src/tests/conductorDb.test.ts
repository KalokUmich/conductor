/**
 * Tests for ConductorDb (SQLite-based metadata storage).
 *
 * Run after compilation:
 *   node --test out/tests/conductorDb.test.js
 */
import { describe, it, beforeEach, afterEach } from 'node:test';
import * as assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as os from 'node:os';

import { ConductorDb, FileMeta, SymbolRow, SymbolVectorRow } from '../services/conductorDb';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

let tmpDir: string;
let dbPath: string;
let db: ConductorDb;

function freshDb(): ConductorDb {
    dbPath = path.join(tmpDir, 'cache.db');
    return new ConductorDb(dbPath);
}

function makeVectorRow(symbolId: string, dim = 4, model = 'cohere.embed-v4', sha1 = 'abc'): SymbolVectorRow {
    const f32 = new Float32Array(dim).fill(0.5);
    return {
        symbol_id: symbolId,
        dim,
        vector: Buffer.from(f32.buffer),
        model,
        sha1,
    };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('ConductorDb', () => {
    beforeEach(() => {
        tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'conductor-db-test-'));
        db = freshDb();
    });

    afterEach(() => {
        try { db.close(); } catch { /* already closed in some tests */ }
        fs.rmSync(tmpDir, { recursive: true, force: true });
    });

    // -----------------------------------------------------------------------
    // Initialization
    // -----------------------------------------------------------------------

    describe('initialization', () => {
        it('opens with WAL journal mode', () => {
            const Database = require('better-sqlite3');
            const raw = new Database(dbPath, { readonly: true });
            const result = raw.pragma('journal_mode') as { journal_mode: string }[];
            raw.close();
            assert.equal(result[0].journal_mode, 'wal');
        });

        it('creates all 5 tables', () => {
            db.selfCheck();
            const Database = require('better-sqlite3');
            const raw = new Database(dbPath, { readonly: true });
            const rows = raw.prepare(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
            ).all() as { name: string }[];
            raw.close();
            const names = rows.map((r: { name: string }) => r.name);
            for (const t of ['files', 'symbols', 'symbol_vectors', 'lsp_defs', 'lsp_refs']) {
                assert.ok(names.includes(t), `Table '${t}' should exist`);
            }
        });

        it('migrates old FAISS symbol_vectors schema to cloud embedding schema', () => {
            // Simulate a pre-existing DB with the old FAISS schema.
            db.close();
            const Database = require('better-sqlite3');
            const raw = new Database(dbPath);
            raw.exec('DROP TABLE symbol_vectors');
            raw.exec(`
                CREATE TABLE symbol_vectors (
                    symbol_id TEXT PRIMARY KEY,
                    faiss_id  INTEGER NOT NULL,
                    dim       INTEGER NOT NULL
                )
            `);
            raw.exec("INSERT INTO symbol_vectors VALUES ('s1', 42, 128)");
            raw.close();

            // Re-open — migration should run automatically.
            const migrated = new ConductorDb(dbPath);
            // Old FAISS data is gone; new schema should work.
            const row = makeVectorRow('s2', 4);
            assert.doesNotThrow(() => migrated.upsertSymbolVector(row));
            assert.ok(migrated.getSymbolVector('s2') !== null);
            migrated.close();
        });
    });

    // -----------------------------------------------------------------------
    // Files
    // -----------------------------------------------------------------------

    describe('upsertFiles / getFilesNeedingReindex', () => {
        it('inserts files and returns those needing reindex', () => {
            const now = Date.now();
            const files: FileMeta[] = [
                { path: '/a.ts', mtime: now, size: 100, lang: 'typescript', sha1: '', last_indexed_at: null },
                { path: '/b.ts', mtime: now, size: 200, lang: 'typescript', sha1: 'abc', last_indexed_at: now },
                { path: '/c.py', mtime: now + 1, size: 50, lang: 'python', sha1: 'def', last_indexed_at: now },
            ];
            db.upsertFiles(files);

            const stale = db.getFilesNeedingReindex();
            const stalePaths = stale.map(f => f.path);
            assert.ok(stalePaths.includes('/a.ts'));
            assert.ok(!stalePaths.includes('/b.ts'));
            assert.ok(stalePaths.includes('/c.py'));
        });

        it('upsert replaces existing rows', () => {
            const files: FileMeta[] = [
                { path: '/a.ts', mtime: 1, size: 10, lang: 'ts', sha1: 'aaa', last_indexed_at: null },
            ];
            db.upsertFiles(files);
            db.upsertFiles([{ path: '/a.ts', mtime: 2, size: 20, lang: 'ts', sha1: 'bbb', last_indexed_at: 2 }]);

            const stale = db.getFilesNeedingReindex();
            assert.equal(stale.length, 0);
        });
    });

    // -----------------------------------------------------------------------
    // Symbols
    // -----------------------------------------------------------------------

    describe('replaceSymbolsForFile', () => {
        const sym1: SymbolRow = {
            id: 'sym-1', path: '/a.ts', name: 'foo', kind: 'function',
            start_line: 1, end_line: 10, signature: 'function foo()',
        };
        const sym2: SymbolRow = {
            id: 'sym-2', path: '/a.ts', name: 'bar', kind: 'function',
            start_line: 12, end_line: 20, signature: 'function bar()',
        };
        const sym3: SymbolRow = {
            id: 'sym-3', path: '/a.ts', name: 'baz', kind: 'class',
            start_line: 22, end_line: 50, signature: 'class Baz',
        };

        it('inserts symbols for a file', () => {
            db.replaceSymbolsForFile('/a.ts', [sym1, sym2]);
            const rows = db.getSymbolsForFile('/a.ts');
            assert.equal(rows.length, 2);
            assert.ok(rows.some(r => r.name === 'foo'));
            assert.ok(rows.some(r => r.name === 'bar'));
        });

        it('replaces (not appends) symbols on second call', () => {
            db.replaceSymbolsForFile('/a.ts', [sym1, sym2]);
            db.replaceSymbolsForFile('/a.ts', [sym3]);

            const rows = db.getSymbolsForFile('/a.ts');
            assert.equal(rows.length, 1);
            assert.equal(rows[0].name, 'baz');
        });

        it('does not affect symbols for other files', () => {
            const other: SymbolRow = {
                id: 'other-1', path: '/b.ts', name: 'other', kind: 'function',
                start_line: 1, end_line: 5, signature: 'function other()',
            };
            db.replaceSymbolsForFile('/a.ts', [sym1]);
            db.replaceSymbolsForFile('/b.ts', [other]);
            db.replaceSymbolsForFile('/a.ts', [sym2]); // replace a.ts only

            assert.equal(db.getSymbolsForFile('/b.ts').length, 1);
        });
    });

    describe('getSymbolsForFile', () => {
        it('returns empty array when file has no symbols', () => {
            assert.deepEqual(db.getSymbolsForFile('/nonexistent.ts'), []);
        });
    });

    // -----------------------------------------------------------------------
    // getSymbolByPathAndName
    // -----------------------------------------------------------------------

    describe('getSymbolByPathAndName', () => {
        const sym1: SymbolRow = {
            id: 'sym-pn-1', path: 'app/models.py', name: 'LoanRequest', kind: 'class',
            start_line: 10, end_line: 25, signature: 'class LoanRequest(BaseModel)',
        };
        const sym2: SymbolRow = {
            id: 'sym-pn-2', path: 'app/models.py', name: 'LoanResponse', kind: 'class',
            start_line: 30, end_line: 45, signature: 'class LoanResponse(BaseModel)',
        };
        const sym3: SymbolRow = {
            id: 'sym-pn-3', path: 'app/service.py', name: 'LoanRequest', kind: 'class',
            start_line: 5, end_line: 15, signature: 'class LoanRequest',
        };

        it('finds a symbol by exact path and name', () => {
            db.replaceSymbolsForFile('app/models.py', [sym1, sym2]);
            const result = db.getSymbolByPathAndName('app/models.py', 'LoanRequest');
            assert.ok(result !== null);
            assert.equal(result.name, 'LoanRequest');
            assert.equal(result.path, 'app/models.py');
            assert.equal(result.start_line, 10);
        });

        it('returns null when name does not exist in the given path', () => {
            db.replaceSymbolsForFile('app/models.py', [sym1]);
            const result = db.getSymbolByPathAndName('app/models.py', 'NonExistent');
            assert.equal(result, null);
        });

        it('returns null when path does not exist', () => {
            db.replaceSymbolsForFile('app/models.py', [sym1]);
            const result = db.getSymbolByPathAndName('app/other.py', 'LoanRequest');
            assert.equal(result, null);
        });

        it('distinguishes same-named symbols across different files', () => {
            db.replaceSymbolsForFile('app/models.py', [sym1]);
            db.replaceSymbolsForFile('app/service.py', [sym3]);

            const fromModels = db.getSymbolByPathAndName('app/models.py', 'LoanRequest');
            const fromService = db.getSymbolByPathAndName('app/service.py', 'LoanRequest');

            assert.ok(fromModels !== null);
            assert.ok(fromService !== null);
            assert.equal(fromModels.start_line, 10);
            assert.equal(fromService.start_line, 5);
        });
    });

    // -----------------------------------------------------------------------
    // Symbol vectors (cloud embeddings)
    // -----------------------------------------------------------------------

    describe('upsertSymbolVector / getSymbolVector', () => {
        it('round-trips a vector row', () => {
            const row = makeVectorRow('sym-a', 4, 'cohere.embed-v4', 'sha123');
            db.upsertSymbolVector(row);
            const got = db.getSymbolVector('sym-a');

            assert.ok(got !== null);
            assert.equal(got.symbol_id, 'sym-a');
            assert.equal(got.dim, 4);
            assert.equal(got.model, 'cohere.embed-v4');
            assert.equal(got.sha1, 'sha123');
        });

        it('vector bytes survive the round-trip', () => {
            const f32 = new Float32Array([1.5, -2.0, 0.0, 4.25]);
            const row: SymbolVectorRow = {
                symbol_id: 'sym-b', dim: 4,
                vector: Buffer.from(f32.buffer),
                model: 'cohere.embed-v4', sha1: 'abc',
            };
            db.upsertSymbolVector(row);

            const got = db.getSymbolVector('sym-b')!;
            const recovered = new Float32Array(got.vector.buffer, got.vector.byteOffset, got.dim);
            assert.deepEqual(Array.from(recovered), [1.5, -2.0, 0.0, 4.25]);
        });

        it('returns null for an unknown symbol ID', () => {
            assert.equal(db.getSymbolVector('unknown'), null);
        });

        it('overwrites on re-insert with same symbol_id', () => {
            db.upsertSymbolVector(makeVectorRow('s', 4, 'model-v1', 'sha-old'));
            db.upsertSymbolVector(makeVectorRow('s', 4, 'model-v2', 'sha-new'));
            const got = db.getSymbolVector('s')!;
            assert.equal(got.model, 'model-v2');
            assert.equal(got.sha1, 'sha-new');
        });
    });

    // -----------------------------------------------------------------------
    // needsEmbedding
    // -----------------------------------------------------------------------

    describe('needsEmbedding', () => {
        it('returns true when no vector exists', () => {
            assert.equal(db.needsEmbedding('new-sym', 'sha', 'model'), true);
        });

        it('returns false when sha1 and model match', () => {
            db.upsertSymbolVector(makeVectorRow('s', 4, 'model-v1', 'sha-abc'));
            assert.equal(db.needsEmbedding('s', 'sha-abc', 'model-v1'), false);
        });

        it('returns true when sha1 changes (content updated)', () => {
            db.upsertSymbolVector(makeVectorRow('s', 4, 'model-v1', 'sha-old'));
            assert.equal(db.needsEmbedding('s', 'sha-new', 'model-v1'), true);
        });

        it('returns true when model changes (trigger re-embedding)', () => {
            db.upsertSymbolVector(makeVectorRow('s', 4, 'model-v1', 'sha-abc'));
            assert.equal(db.needsEmbedding('s', 'sha-abc', 'model-v2'), true);
        });

        it('returns true when both sha1 and model change', () => {
            db.upsertSymbolVector(makeVectorRow('s', 4, 'model-v1', 'sha-old'));
            assert.equal(db.needsEmbedding('s', 'sha-new', 'model-v2'), true);
        });
    });

    // -----------------------------------------------------------------------
    // LSP cache
    // -----------------------------------------------------------------------

    describe('LSP definition cache', () => {
        it('round-trips a cached definition', () => {
            const payload = { uri: 'file:///a.ts', range: { start: 1, end: 5 } };
            db.cacheLspDef('def:foo', payload);
            const result = db.getLspDef('def:foo');
            assert.deepEqual(result, payload);
        });

        it('returns null for a missing key', () => {
            assert.equal(db.getLspDef('nonexistent'), null);
        });

        it('overwrites on duplicate key', () => {
            db.cacheLspDef('k', { v: 1 });
            db.cacheLspDef('k', { v: 2 });
            assert.deepEqual(db.getLspDef('k'), { v: 2 });
        });
    });

    describe('LSP references cache', () => {
        it('round-trips cached references', () => {
            const payload = [{ uri: 'file:///b.ts', line: 10 }];
            db.cacheLspRefs('refs:bar', payload);
            const result = db.getLspRefs('refs:bar');
            assert.deepEqual(result, payload);
        });

        it('returns null for a missing key', () => {
            assert.equal(db.getLspRefs('nope'), null);
        });
    });

    // -----------------------------------------------------------------------
    // Lifecycle
    // -----------------------------------------------------------------------

    describe('selfCheck', () => {
        it('passes on a valid DB', () => {
            assert.doesNotThrow(() => db.selfCheck());
        });
    });

    describe('close', () => {
        it('closes without error', () => {
            assert.doesNotThrow(() => db.close());
        });
    });
});
