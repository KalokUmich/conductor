/**
 * Unit tests for backendHealthCheck.
 *
 * Uses a real HTTP server (node:http) so there are no mocking libraries needed.
 *
 * Run after compilation:
 *   node --test out/tests/backendHealthCheck.test.js
 */
import { describe, it, afterEach } from 'node:test';
import * as assert from 'node:assert/strict';
import * as http from 'node:http';

import { checkBackendHealth } from '../services/backendHealthCheck';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Start a tiny HTTP server that responds to GET /health. */
function startServer(
    statusCode: number,
    body: string = '{"status":"ok"}',
): Promise<{ url: string; server: http.Server }> {
    return new Promise((resolve) => {
        const server = http.createServer((_req, res) => {
            res.writeHead(statusCode, { 'Content-Type': 'application/json' });
            res.end(body);
        });
        server.listen(0, '127.0.0.1', () => {
            const addr = server.address() as { port: number };
            resolve({ url: `http://127.0.0.1:${addr.port}`, server });
        });
    });
}

/** Close a server, ignoring errors if already closed. */
function closeServer(server: http.Server): Promise<void> {
    return new Promise((resolve) => {
        server.close(() => resolve());
    });
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('checkBackendHealth', () => {
    let server: http.Server | null = null;

    afterEach(async () => {
        if (server) {
            await closeServer(server);
            server = null;
        }
    });

    it('returns true when /health responds with 200', async () => {
        const s = await startServer(200);
        server = s.server;

        const result = await checkBackendHealth(s.url);
        assert.equal(result, true);
    });

    it('returns false when /health responds with 500', async () => {
        const s = await startServer(500, '{"status":"error"}');
        server = s.server;

        const result = await checkBackendHealth(s.url);
        assert.equal(result, false);
    });

    it('returns false when /health responds with 503', async () => {
        const s = await startServer(503, '{"status":"unavailable"}');
        server = s.server;

        const result = await checkBackendHealth(s.url);
        assert.equal(result, false);
    });

    it('returns false when server is unreachable', async () => {
        // Port 1 is almost certainly not listening
        const result = await checkBackendHealth('http://127.0.0.1:1');
        assert.equal(result, false);
    });

    it('returns false on timeout', async () => {
        // Create a server that never responds
        const slowServer = http.createServer(() => {
            // intentionally do nothing – request hangs
        });

        await new Promise<void>((resolve) => {
            slowServer.listen(0, '127.0.0.1', () => resolve());
        });
        server = slowServer;

        const addr = slowServer.address() as { port: number };
        const url = `http://127.0.0.1:${addr.port}`;

        const result = await checkBackendHealth(url, { timeoutMs: 200 });
        assert.equal(result, false);
    });

    it('returns true for 200 with custom timeout', async () => {
        const s = await startServer(200);
        server = s.server;

        const result = await checkBackendHealth(s.url, { timeoutMs: 10_000 });
        assert.equal(result, true);
    });
});

