/**
 * Tests for EmbeddingClient.
 *
 * Spins up a local HTTP server to exercise the actual fetch() path.
 *
 * Run after compilation:
 *   node --test out/tests/embeddingClient.test.js
 */
import { describe, it, beforeEach, afterEach } from 'node:test';
import * as assert from 'node:assert/strict';
import * as http from 'node:http';

import { EmbeddingClient } from '../services/embeddingClient';

// ---------------------------------------------------------------------------
// Minimal HTTP server helper
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

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('EmbeddingClient', () => {
    // -----------------------------------------------------------------------
    // embed()
    // -----------------------------------------------------------------------

    describe('embed', () => {
        let server: { url: string; close: () => Promise<void> } | undefined;

        afterEach(async () => {
            if (server) { await server.close(); server = undefined; }
        });

        it('returns vectors from a successful response', async () => {
            const vectors = [[0.1, 0.2], [0.3, 0.4]];
            server = await startServer({
                status: 200,
                body: { vectors, model: 'cohere.embed-v4', dim: 2 },
            });
            const client = new EmbeddingClient(server.url);
            const result = await client.embed(['hello', 'world']);
            assert.deepEqual(result, vectors);
        });

        it('sends texts in the request body', async () => {
            let receivedBody = '';
            const srv = http.createServer((req, res) => {
                let data = '';
                req.on('data', c => { data += c; });
                req.on('end', () => {
                    receivedBody = data;
                    res.writeHead(200, { 'Content-Type': 'application/json' });
                    res.end(JSON.stringify({ vectors: [[0.1]], model: 'm', dim: 1 }));
                });
            });
            await new Promise<void>(r => srv.listen(0, '127.0.0.1', r));
            const port = (srv.address() as { port: number }).port;
            // Assign to outer `server` so afterEach closes it.
            server = {
                url: `http://127.0.0.1:${port}`,
                close: () => new Promise<void>((r, j) => srv.close(e => e ? j(e) : r())),
            };

            const client = new EmbeddingClient(server.url);
            await client.embed(['my text']);

            const parsed = JSON.parse(receivedBody);
            assert.deepEqual(parsed.texts, ['my text']);
        });

        it('throws on HTTP 503', async () => {
            server = await startServer({ status: 503, body: { error: 'Service Unavailable' } });
            const client = new EmbeddingClient(server.url);
            await assert.rejects(
                client.embed(['x']),
                /503/,
            );
        });

        it('throws on HTTP 500 with body text', async () => {
            server = await startServer({ status: 500, body: 'Internal Server Error' });
            const client = new EmbeddingClient(server.url);
            await assert.rejects(
                client.embed(['x']),
                /500/,
            );
        });

        it('throws when vectors field is missing', async () => {
            server = await startServer({ status: 200, body: { model: 'm', dim: 1 } });
            const client = new EmbeddingClient(server.url);
            await assert.rejects(
                client.embed(['x']),
                /vectors/,
            );
        });

        it('throws when vector count mismatches text count', async () => {
            server = await startServer({
                status: 200,
                body: { vectors: [[0.1], [0.2]], model: 'm', dim: 1 }, // 2 vectors for 1 text
            });
            const client = new EmbeddingClient(server.url);
            await assert.rejects(
                client.embed(['only-one-text']),
                /1 texts/,
            );
        });

        it('throws on network error (unreachable host)', async () => {
            const client = new EmbeddingClient('http://127.0.0.1:1'); // port 1 is unreachable
            await assert.rejects(
                client.embed(['x']),
                /Network error/,
            );
        });
    });

    // -----------------------------------------------------------------------
    // toFloat32Array()
    // -----------------------------------------------------------------------

    describe('toFloat32Array', () => {
        const client = new EmbeddingClient('http://unused');

        it('converts a number array to Float32Array', () => {
            const result = client.toFloat32Array([1.0, 2.0, 3.0]);
            assert.ok(result instanceof Float32Array);
            assert.equal(result.length, 3);
        });

        it('values are preserved (within float32 precision)', () => {
            const input = [0.5, -1.0, 0.0, 1000.0];
            const result = client.toFloat32Array(input);
            for (let i = 0; i < input.length; i++) {
                assert.ok(Math.abs(result[i] - input[i]) < 1e-4);
            }
        });

        it('handles an empty array', () => {
            const result = client.toFloat32Array([]);
            assert.equal(result.length, 0);
        });

        it('produces a new array each time (no shared memory)', () => {
            const v = [1.0, 2.0];
            const a = client.toFloat32Array(v);
            const b = client.toFloat32Array(v);
            a[0] = 99;
            assert.equal(b[0], 1.0); // b is independent
        });
    });
});
