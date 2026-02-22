/**
 * Tests for RagClient.
 *
 * Spins up a local HTTP server to exercise the actual fetch() path.
 *
 * Run after compilation:
 *   node --test out/tests/ragClient.test.js
 */
import { describe, it, afterEach } from 'node:test';
import * as assert from 'node:assert/strict';
import * as http from 'node:http';

import { RagClient } from '../services/ragClient';

// ---------------------------------------------------------------------------
// Minimal HTTP server helpers
// ---------------------------------------------------------------------------

interface ServerSpec {
    status: number;
    body: unknown;
}

function startServer(spec: ServerSpec): Promise<{ url: string; close: () => Promise<void> }> {
    return new Promise((resolve, reject) => {
        const server = http.createServer((_req, res) => {
            res.writeHead(spec.status, { 'Content-Type': 'application/json' });
            res.end(typeof spec.body === 'string' ? spec.body : JSON.stringify(spec.body));
        });
        server.listen(0, '127.0.0.1', () => {
            const addr = server.address() as { port: number };
            resolve({
                url: `http://127.0.0.1:${addr.port}`,
                close: () => new Promise<void>((res, rej) =>
                    server.close(err => (err ? rej(err) : res())),
                ),
            });
        });
        server.on('error', reject);
    });
}

/** Start a server that captures the request body before responding. */
function startCapturingServer(
    spec: ServerSpec,
): Promise<{ url: string; close: () => Promise<void>; getBody: () => string; getPath: () => string }> {
    let body = '';
    let reqPath = '';
    return new Promise((resolve, reject) => {
        const server = http.createServer((req, res) => {
            reqPath = req.url ?? '';
            let data = '';
            req.on('data', c => { data += c; });
            req.on('end', () => {
                body = data;
                res.writeHead(spec.status, { 'Content-Type': 'application/json' });
                res.end(typeof spec.body === 'string' ? spec.body : JSON.stringify(spec.body));
            });
        });
        server.listen(0, '127.0.0.1', () => {
            const addr = server.address() as { port: number };
            resolve({
                url: `http://127.0.0.1:${addr.port}`,
                close: () => new Promise<void>((r, j) => server.close(e => e ? j(e) : r())),
                getBody: () => body,
                getPath: () => reqPath,
            });
        });
        server.on('error', reject);
    });
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('RagClient', () => {
    let server: { url: string; close: () => Promise<void> } | undefined;

    afterEach(async () => {
        if (server) { await server.close(); server = undefined; }
    });

    // -----------------------------------------------------------------------
    // index()
    // -----------------------------------------------------------------------

    describe('index', () => {
        it('sends files to /rag/index and returns response', async () => {
            const response = { chunks_added: 5, chunks_removed: 0, files_processed: 1 };
            const srv = await startCapturingServer({ status: 200, body: response });
            server = srv;

            const client = new RagClient(srv.url);
            const result = await client.index('ws1', [
                { path: 'a.py', content: 'x = 1', action: 'upsert' },
            ]);

            assert.equal(result.chunks_added, 5);
            assert.equal(result.files_processed, 1);
            assert.equal(srv.getPath(), '/rag/index');

            const body = JSON.parse(srv.getBody());
            assert.equal(body.workspace_id, 'ws1');
            assert.equal(body.files.length, 1);
            assert.equal(body.files[0].path, 'a.py');
        });

        it('throws on HTTP 503', async () => {
            server = await startServer({ status: 503, body: { error: 'not configured' } });
            const client = new RagClient(server.url);
            await assert.rejects(
                client.index('ws1', [{ path: 'a.py', content: 'x', action: 'upsert' }]),
                /503/,
            );
        });
    });

    // -----------------------------------------------------------------------
    // reindex()
    // -----------------------------------------------------------------------

    describe('reindex', () => {
        it('sends files to /rag/reindex and returns response', async () => {
            const response = { chunks_added: 10, chunks_removed: 3, files_processed: 2 };
            const srv = await startCapturingServer({ status: 200, body: response });
            server = srv;

            const client = new RagClient(srv.url);
            const result = await client.reindex('ws1', [
                { path: 'a.py', content: 'x = 1', action: 'upsert' },
                { path: 'b.py', content: 'y = 2', action: 'upsert' },
            ]);

            assert.equal(result.chunks_added, 10);
            assert.equal(result.chunks_removed, 3);
            assert.equal(srv.getPath(), '/rag/reindex');
        });
    });

    // -----------------------------------------------------------------------
    // search()
    // -----------------------------------------------------------------------

    describe('search', () => {
        it('sends query to /rag/search and returns results', async () => {
            const response = {
                results: [{
                    file_path: 'src/main.py',
                    start_line: 1,
                    end_line: 10,
                    symbol_name: 'main',
                    symbol_type: 'function',
                    content: 'def main(): ...',
                    score: 0.95,
                    language: 'python',
                }],
                query: 'auth handler',
                workspace_id: 'ws1',
            };
            const srv = await startCapturingServer({ status: 200, body: response });
            server = srv;

            const client = new RagClient(srv.url);
            const result = await client.search('ws1', 'auth handler', 5);

            assert.equal(result.results.length, 1);
            assert.equal(result.results[0].file_path, 'src/main.py');
            assert.equal(result.query, 'auth handler');
            assert.equal(srv.getPath(), '/rag/search');

            const body = JSON.parse(srv.getBody());
            assert.equal(body.workspace_id, 'ws1');
            assert.equal(body.query, 'auth handler');
            assert.equal(body.top_k, 5);
        });

        it('sends filters when provided', async () => {
            const response = { results: [], query: 'test', workspace_id: 'ws1' };
            const srv = await startCapturingServer({ status: 200, body: response });
            server = srv;

            const client = new RagClient(srv.url);
            await client.search('ws1', 'test', 10, {
                languages: ['python'],
                file_patterns: ['src/*.py'],
            });

            const body = JSON.parse(srv.getBody());
            assert.deepEqual(body.filters, {
                languages: ['python'],
                file_patterns: ['src/*.py'],
            });
        });

        it('omits optional fields when not provided', async () => {
            const response = { results: [], query: 'test', workspace_id: 'ws1' };
            const srv = await startCapturingServer({ status: 200, body: response });
            server = srv;

            const client = new RagClient(srv.url);
            await client.search('ws1', 'test');

            const body = JSON.parse(srv.getBody());
            assert.equal(body.top_k, undefined);
            assert.equal(body.filters, undefined);
        });

        it('throws on HTTP 503', async () => {
            server = await startServer({ status: 503, body: { error: 'not configured' } });
            const client = new RagClient(server.url);
            await assert.rejects(
                client.search('ws1', 'test'),
                /503/,
            );
        });
    });

    // -----------------------------------------------------------------------
    // Error handling
    // -----------------------------------------------------------------------

    describe('error handling', () => {
        it('throws on network error (unreachable host)', async () => {
            const client = new RagClient('http://127.0.0.1:1');
            await assert.rejects(
                client.index('ws1', [{ path: 'a.py', content: 'x', action: 'upsert' }]),
                /Network error/,
            );
        });

        it('throws on HTTP 500 with error message', async () => {
            server = await startServer({ status: 500, body: { error: 'disk full' } });
            const client = new RagClient(server.url);
            await assert.rejects(
                client.index('ws1', [{ path: 'a.py', content: 'x', action: 'upsert' }]),
                /500/,
            );
        });

        it('strips trailing slash from base URL', async () => {
            const response = { chunks_added: 1, chunks_removed: 0, files_processed: 1 };
            const srv = await startCapturingServer({ status: 200, body: response });
            server = srv;

            const client = new RagClient(srv.url + '/');
            await client.index('ws1', [{ path: 'a.py', content: 'x', action: 'upsert' }]);

            // Path should not have double slashes
            assert.equal(srv.getPath(), '/rag/index');
        });
    });
});
