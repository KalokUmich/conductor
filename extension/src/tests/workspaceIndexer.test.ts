/**
 * Tests for workspaceIndexer — indexWorkspace two-phase pipeline.
 *
 * Run after compilation:
 *   node --test out/tests/workspaceIndexer.test.js
 */
import { describe, it, beforeEach, afterEach } from 'node:test';
import * as assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as os from 'node:os';

import { indexWorkspace, IndexProgress } from '../services/workspaceIndexer';
import { ConductorDb } from '../services/conductorDb';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

let tmpDir: string;

/** Write a file relative to tmpDir, creating parent dirs as needed. */
function write(relPath: string, content: string): string {
    const abs = path.join(tmpDir, relPath);
    fs.mkdirSync(path.dirname(abs), { recursive: true });
    fs.writeFileSync(abs, content, 'utf-8');
    return abs;
}

/** Open a ConductorDb at the default .conductor/cache.db location. */
function openDb(): ConductorDb {
    const conductorDir = path.join(tmpDir, '.conductor');
    fs.mkdirSync(conductorDir, { recursive: true });
    return new ConductorDb(path.join(conductorDir, 'cache.db'));
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('indexWorkspace', () => {
    beforeEach(() => {
        tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'conductor-indexer-test-'));
    });

    afterEach(() => {
        fs.rmSync(tmpDir, { recursive: true, force: true });
    });

    it('Phase 1 scans files and populates the database', async () => {
        write('src/index.ts', 'export function greet() { return "hello"; }');
        write('src/utils.ts', 'export const add = (a: number, b: number) => a + b;');

        const db = openDb();
        try {
            const result = await indexWorkspace(tmpDir, db, {
                embeddingModel: 'test-model',
                embeddingDim: 128,
                backendUrl: 'http://127.0.0.1:9999', // not called during Phase 1
                phase1TimeoutMs: 5000,
            });

            assert.ok(result.filesScanned >= 2, `expected >= 2 files, got ${result.filesScanned}`);

            const files = db.getAllFiles();
            const paths = files.map(f => f.path);
            assert.ok(paths.some(p => p.includes('index.ts')));
            assert.ok(paths.some(p => p.includes('utils.ts')));
        } finally {
            db.close();
        }
    });

    it('returns progress with phase = extracting after Phase 1', async () => {
        write('app.py', 'def main():\n    pass\n');

        const db = openDb();
        try {
            const result = await indexWorkspace(tmpDir, db, {
                embeddingModel: 'test-model',
                embeddingDim: 128,
                backendUrl: 'http://127.0.0.1:9999',
                phase1TimeoutMs: 5000,
            });

            // The returned snapshot is taken right after Phase 1 completes.
            // Phase 2 runs asynchronously and may complete very quickly for
            // small workspaces, so accept either 'extracting' or 'done'.
            assert.ok(
                result.phase === 'extracting' || result.phase === 'done',
                `expected 'extracting' or 'done', got '${result.phase}'`,
            );
        } finally {
            db.close();
        }
    });

    it('reports progress via onProgress callback', async () => {
        write('index.ts', 'export function hello() {}');

        const db = openDb();
        const progressUpdates: IndexProgress[] = [];

        try {
            await indexWorkspace(tmpDir, db, {
                embeddingModel: 'test-model',
                embeddingDim: 128,
                backendUrl: 'http://127.0.0.1:9999',
                phase1TimeoutMs: 5000,
                onProgress: (p) => {
                    progressUpdates.push({ ...p });
                },
            });

            // Should have received at least the initial 'scanning' and
            // the post-Phase-1 'extracting' updates.
            assert.ok(progressUpdates.length >= 2, `expected >= 2 updates, got ${progressUpdates.length}`);
            assert.equal(progressUpdates[0].phase, 'scanning');
        } finally {
            db.close();
        }
    });

    it('completes without error on an empty workspace', async () => {
        const db = openDb();
        try {
            const result = await indexWorkspace(tmpDir, db, {
                embeddingModel: 'test-model',
                embeddingDim: 128,
                backendUrl: 'http://127.0.0.1:9999',
                phase1TimeoutMs: 5000,
            });

            assert.equal(result.filesScanned, 0);
        } finally {
            db.close();
        }
    });

    it('Phase 1 respects timeout and does not throw', async () => {
        // Create enough files to make scanning non-trivial.
        for (let i = 0; i < 10; i++) {
            write(`src/file${i}.ts`, `export const x${i} = ${i};`);
        }

        const db = openDb();
        try {
            // Very short timeout — Phase 1 may or may not complete fully
            // but should not reject.
            const result = await indexWorkspace(tmpDir, db, {
                embeddingModel: 'test-model',
                embeddingDim: 128,
                backendUrl: 'http://127.0.0.1:9999',
                phase1TimeoutMs: 1,  // 1ms — likely times out
            });

            // Should still return a valid progress object.
            assert.equal(typeof result.filesScanned, 'number');
            assert.equal(typeof result.phase, 'string');
        } finally {
            db.close();
        }
    });

    it('extracts symbols during Phase 2 for stale files', async () => {
        write('lib.ts', [
            'export function add(a: number, b: number): number { return a + b; }',
            'export class Calculator { multiply(a: number, b: number) { return a * b; } }',
        ].join('\n'));

        const db = openDb();
        try {
            await indexWorkspace(tmpDir, db, {
                embeddingModel: 'test-model',
                embeddingDim: 128,
                backendUrl: 'http://127.0.0.1:9999',
                phase1TimeoutMs: 5000,
            });

            // Give Phase 2 a moment to run (fire-and-forget).
            await new Promise(r => setTimeout(r, 200));

            // Check that symbols were extracted and stored.
            const symbols = db.getSymbolsForFile('lib.ts');
            const names = symbols.map(s => s.name);
            assert.ok(names.includes('add'), `expected 'add' in symbols, got: ${names}`);
            assert.ok(names.includes('Calculator'), `expected 'Calculator' in symbols, got: ${names}`);
        } finally {
            db.close();
        }
    });

    it('does not scan .conductor directory', async () => {
        // Create a file inside .conductor/ that should be ignored.
        write('.conductor/internal.ts', 'export const secret = "hidden";');
        write('src/app.ts', 'export const visible = true;');

        const db = openDb();
        try {
            await indexWorkspace(tmpDir, db, {
                embeddingModel: 'test-model',
                embeddingDim: 128,
                backendUrl: 'http://127.0.0.1:9999',
                phase1TimeoutMs: 5000,
            });

            const files = db.getAllFiles();
            const paths = files.map(f => f.path);
            assert.ok(!paths.some(p => p.includes('.conductor')), '.conductor files should be excluded');
            assert.ok(paths.some(p => p.includes('app.ts')), 'src/app.ts should be included');
        } finally {
            db.close();
        }
    });
});
