/**
 * Tests for connectionDiagnostics helpers.
 *
 * Run after compilation:
 *   node --test out/tests/connectionDiagnostics.test.js
 */
import { describe, it } from 'node:test';
import * as assert from 'node:assert/strict';

import {
    diagnoseBackendConnection,
    selectPreferredNgrokUrl,
} from '../services/connectionDiagnostics';

describe('selectPreferredNgrokUrl', () => {
    it('prefers the HTTPS tunnel targeting port 8000', () => {
        const result = selectPreferredNgrokUrl([
            { publicUrl: 'https://fallback.ngrok.app', proto: 'https', config: { addr: 'http://localhost:3000' } },
            { publicUrl: 'https://backend.ngrok.app', proto: 'https', config: { addr: 'http://localhost:8000' } },
        ]);

        assert.equal(result, 'https://backend.ngrok.app');
    });

    it('falls back to any HTTPS tunnel when no backend tunnel exists', () => {
        const result = selectPreferredNgrokUrl([
            { publicUrl: 'http://plain.ngrok.app', proto: 'http', config: { addr: 'http://localhost:8000' } },
            { publicUrl: 'https://fallback.ngrok.app', proto: 'https', config: { addr: 'http://localhost:3000' } },
        ]);

        assert.equal(result, 'https://fallback.ngrok.app');
    });

    it('returns null when no HTTPS tunnel is available', () => {
        const result = selectPreferredNgrokUrl([
            { publicUrl: 'http://plain.ngrok.app', proto: 'http', config: { addr: 'http://localhost:8000' } },
        ]);

        assert.equal(result, null);
    });
});

describe('diagnoseBackendConnection', () => {
    it('reports backend unreachable during initial connection', () => {
        const result = diagnoseBackendConnection({
            backendUrl: 'http://localhost:8000',
            backendHealthy: false,
            hasConnectedBefore: false,
            reconnectAttempts: 1,
            maxReconnectAttempts: 10,
        });

        assert.equal(result.isError, true);
        assert.match(result.status, /Backend unreachable/);
        assert.match(result.status, /Retrying/);
    });

    it('reports chat socket failure when backend is healthy but initial connect fails', () => {
        const result = diagnoseBackendConnection({
            backendUrl: 'http://localhost:8000',
            backendHealthy: true,
            hasConnectedBefore: false,
            reconnectAttempts: 1,
            maxReconnectAttempts: 10,
        });

        assert.equal(result.isError, false);
        assert.match(result.status, /chat socket could not be established/);
    });

    it('reports reconnecting after a previously successful connection drops', () => {
        const result = diagnoseBackendConnection({
            backendUrl: 'http://localhost:8000',
            backendHealthy: true,
            hasConnectedBefore: true,
            reconnectAttempts: 3,
            maxReconnectAttempts: 10,
        });

        assert.equal(result.isError, false);
        assert.match(result.status, /Connection dropped/);
        assert.match(result.status, /3\/10/);
    });

    it('reports a final lost connection after retries are exhausted', () => {
        const result = diagnoseBackendConnection({
            backendUrl: 'http://localhost:8000',
            backendHealthy: true,
            hasConnectedBefore: true,
            reconnectAttempts: 10,
            maxReconnectAttempts: 10,
        });

        assert.equal(result.isError, true);
        assert.match(result.status, /Unable to reconnect/);
    });
});