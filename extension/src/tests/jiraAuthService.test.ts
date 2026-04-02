/**
 * Unit tests for Jira Auth Service — connection cache + token restore.
 *
 * Run after compilation:
 *   node --test out/tests/jiraAuthService.test.js
 */
import { describe, it } from 'node:test';
import * as assert from 'node:assert/strict';

import {
    JIRA_EXPIRY_MS,
    wrapJiraConnection,
    getValidJiraConnection,
    isJiraConnectionStale,
    restoreJiraFromTokenStore,
} from '../services/jiraAuthService';

// ---------------------------------------------------------------------------
// Mock JiraTokenStore
// ---------------------------------------------------------------------------

function mockTokenStore(returns: any) {
    return {
        getValidTokens: async () => returns,
    } as any;
}

// ---------------------------------------------------------------------------
// wrapJiraConnection (existing behavior)
// ---------------------------------------------------------------------------

describe('wrapJiraConnection', () => {
    it('wraps with provided timestamp', () => {
        const conn = { connected: true, siteUrl: 'https://x.atlassian.net', cloudId: 'c' };
        const result = wrapJiraConnection(conn, 1000);
        assert.deepEqual(result.connection, conn);
        assert.equal(result.storedAt, 1000);
    });
});

// ---------------------------------------------------------------------------
// getValidJiraConnection (existing behavior)
// ---------------------------------------------------------------------------

describe('getValidJiraConnection', () => {
    it('returns connection within expiry', () => {
        const now = Date.now();
        const stored = { connection: { connected: true, siteUrl: 's', cloudId: 'c' }, storedAt: now - 1000 };
        assert.deepEqual(getValidJiraConnection(stored, now), stored.connection);
    });

    it('returns null when expired', () => {
        const now = Date.now();
        const stored = { connection: { connected: true, siteUrl: 's', cloudId: 'c' }, storedAt: now - JIRA_EXPIRY_MS - 1 };
        assert.equal(getValidJiraConnection(stored, now), null);
    });

    it('returns null for falsy input', () => {
        assert.equal(getValidJiraConnection(null), null);
        assert.equal(getValidJiraConnection(undefined), null);
    });

    it('returns null for old format (no storedAt)', () => {
        assert.equal(getValidJiraConnection({ connected: true, siteUrl: 's', cloudId: 'c' }), null);
    });
});

// ---------------------------------------------------------------------------
// isJiraConnectionStale
// ---------------------------------------------------------------------------

describe('isJiraConnectionStale', () => {
    it('returns false for falsy input', () => {
        assert.equal(isJiraConnectionStale(null), false);
    });

    it('returns true for expired wrapper', () => {
        const now = Date.now();
        const stored = { connection: { connected: true, siteUrl: 's', cloudId: 'c' }, storedAt: now - JIRA_EXPIRY_MS - 1 };
        assert.equal(isJiraConnectionStale(stored, now), true);
    });

    it('returns false for valid wrapper', () => {
        const now = Date.now();
        const stored = { connection: { connected: true, siteUrl: 's', cloudId: 'c' }, storedAt: now - 1000 };
        assert.equal(isJiraConnectionStale(stored, now), false);
    });
});

// ---------------------------------------------------------------------------
// restoreJiraFromTokenStore
// ---------------------------------------------------------------------------

describe('restoreJiraFromTokenStore', () => {
    it('returns connection info from valid tokens', async () => {
        const store = mockTokenStore({
            accessToken: 'acc',
            refreshToken: 'ref',
            meta: { expiresAt: Date.now() + 3600000, cloudId: 'cloud-r', siteUrl: 'https://r.atlassian.net' },
        });

        const result = await restoreJiraFromTokenStore(store);
        assert.ok(result);
        assert.equal(result!.connected, true);
        assert.equal(result!.cloudId, 'cloud-r');
        assert.equal(result!.siteUrl, 'https://r.atlassian.net');
    });

    it('returns null when no tokens available', async () => {
        const store = mockTokenStore(null);
        assert.equal(await restoreJiraFromTokenStore(store), null);
    });
});
