/**
 * Unit tests for JiraTokenStore — SecretStorage + .conductor/jira.json persistence.
 *
 * Run after compilation:
 *   node --test out/tests/jiraTokenStore.test.js
 */
import { describe, it, beforeEach } from 'node:test';
import * as assert from 'node:assert/strict';
import * as fs from 'fs/promises';
import * as path from 'path';
import * as os from 'os';

import { JiraTokenStore } from '../services/jiraTokenStore';
import type { JiraTokenMeta } from '../services/jiraTokenStore';

// ---------------------------------------------------------------------------
// Mock SecretStorage
// ---------------------------------------------------------------------------

class MockSecretStorage {
    private _store = new Map<string, string>();

    async get(key: string): Promise<string | undefined> {
        return this._store.get(key);
    }

    async store(key: string, value: string): Promise<void> {
        this._store.set(key, value);
    }

    async delete(key: string): Promise<void> {
        this._store.delete(key);
    }

    // Test helper
    get size(): number {
        return this._store.size;
    }

    // Satisfy the VS Code interface
    onDidChange = { dispose: () => {} } as any;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

let tmpDir: string;
let secrets: MockSecretStorage;

async function makeTmpDir(): Promise<string> {
    return await fs.mkdtemp(path.join(os.tmpdir(), 'jira-token-test-'));
}

async function readMeta(wsRoot: string): Promise<JiraTokenMeta | null> {
    try {
        const raw = await fs.readFile(path.join(wsRoot, '.conductor', 'jira.json'), 'utf-8');
        return JSON.parse(raw);
    } catch {
        return null;
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('JiraTokenStore', () => {
    beforeEach(async () => {
        tmpDir = await makeTmpDir();
        secrets = new MockSecretStorage();
    });

    describe('save', () => {
        it('stores tokens in SecretStorage and metadata in jira.json', async () => {
            const store = new JiraTokenStore(secrets as any, tmpDir, 'http://localhost:8000');

            await store.save('acc-1', 'ref-1', 3600, 'cloud-x', 'https://x.atlassian.net');

            // Check SecretStorage
            assert.equal(await secrets.get('conductor.jira.accessToken'), 'acc-1');
            assert.equal(await secrets.get('conductor.jira.refreshToken'), 'ref-1');

            // Check metadata file
            const meta = await readMeta(tmpDir);
            assert.ok(meta);
            assert.equal(meta!.cloudId, 'cloud-x');
            assert.equal(meta!.siteUrl, 'https://x.atlassian.net');
            assert.ok(meta!.expiresAt > Date.now());
        });

        it('creates .conductor directory if missing', async () => {
            const store = new JiraTokenStore(secrets as any, tmpDir, 'http://localhost:8000');

            await store.save('a', 'r', 3600, 'c', 's');

            const stat = await fs.stat(path.join(tmpDir, '.conductor'));
            assert.ok(stat.isDirectory());
        });
    });

    describe('hasTokens', () => {
        it('returns false when no metadata file exists', async () => {
            const store = new JiraTokenStore(secrets as any, tmpDir, 'http://localhost:8000');
            assert.equal(await store.hasTokens(), false);
        });

        it('returns true after save', async () => {
            const store = new JiraTokenStore(secrets as any, tmpDir, 'http://localhost:8000');
            await store.save('a', 'r', 3600, 'c', 's');
            assert.equal(await store.hasTokens(), true);
        });
    });

    describe('getValidTokens', () => {
        it('returns null when no tokens stored', async () => {
            const store = new JiraTokenStore(secrets as any, tmpDir, 'http://localhost:8000');
            assert.equal(await store.getValidTokens(), null);
        });

        it('returns tokens when not expired', async () => {
            const store = new JiraTokenStore(secrets as any, tmpDir, 'http://localhost:8000');
            await store.save('acc-1', 'ref-1', 3600, 'cloud-x', 'https://x.atlassian.net');

            const result = await store.getValidTokens();
            assert.ok(result);
            assert.equal(result!.accessToken, 'acc-1');
            assert.equal(result!.refreshToken, 'ref-1');
            assert.equal(result!.meta.cloudId, 'cloud-x');
        });

        it('returns null when SecretStorage is empty but meta exists', async () => {
            const store = new JiraTokenStore(secrets as any, tmpDir, 'http://localhost:8000');

            // Write meta but no secrets
            const dir = path.join(tmpDir, '.conductor');
            await fs.mkdir(dir, { recursive: true });
            await fs.writeFile(
                path.join(dir, 'jira.json'),
                JSON.stringify({ expiresAt: Date.now() + 3600000, cloudId: 'c', siteUrl: 's' }),
            );

            assert.equal(await store.getValidTokens(), null);
        });

        it('attempts refresh when token is expired', async () => {
            const store = new JiraTokenStore(secrets as any, tmpDir, 'http://localhost:8000');

            // Save with already-expired token
            await secrets.store('conductor.jira.accessToken', 'old-acc');
            await secrets.store('conductor.jira.refreshToken', 'old-ref');
            const dir = path.join(tmpDir, '.conductor');
            await fs.mkdir(dir, { recursive: true });
            await fs.writeFile(
                path.join(dir, 'jira.json'),
                JSON.stringify({ expiresAt: Date.now() - 1000, cloudId: 'c', siteUrl: 's' }),
            );

            // getValidTokens will try to refresh via fetch (which will fail in test)
            // so it returns null
            const result = await store.getValidTokens();
            assert.equal(result, null);
        });
    });

    describe('getMeta', () => {
        it('returns null when no file exists', async () => {
            const store = new JiraTokenStore(secrets as any, tmpDir, 'http://localhost:8000');
            assert.equal(await store.getMeta(), null);
        });

        it('returns metadata after save', async () => {
            const store = new JiraTokenStore(secrets as any, tmpDir, 'http://localhost:8000');
            await store.save('a', 'r', 7200, 'cloud-m', 'https://m.atlassian.net');

            const meta = await store.getMeta();
            assert.ok(meta);
            assert.equal(meta!.cloudId, 'cloud-m');
            assert.equal(meta!.siteUrl, 'https://m.atlassian.net');
        });

        it('returns null for malformed JSON', async () => {
            const store = new JiraTokenStore(secrets as any, tmpDir, 'http://localhost:8000');
            const dir = path.join(tmpDir, '.conductor');
            await fs.mkdir(dir, { recursive: true });
            await fs.writeFile(path.join(dir, 'jira.json'), '{broken json');

            assert.equal(await store.getMeta(), null);
        });

        it('returns null for JSON missing required fields', async () => {
            const store = new JiraTokenStore(secrets as any, tmpDir, 'http://localhost:8000');
            const dir = path.join(tmpDir, '.conductor');
            await fs.mkdir(dir, { recursive: true });
            await fs.writeFile(path.join(dir, 'jira.json'), '{"cloudId": "c"}');

            assert.equal(await store.getMeta(), null);
        });
    });

    describe('clear', () => {
        it('removes secrets and metadata file', async () => {
            const store = new JiraTokenStore(secrets as any, tmpDir, 'http://localhost:8000');
            await store.save('a', 'r', 3600, 'c', 's');

            await store.clear();

            assert.equal(secrets.size, 0);
            assert.equal(await readMeta(tmpDir), null);
        });

        it('does not throw when nothing to clear', async () => {
            const store = new JiraTokenStore(secrets as any, tmpDir, 'http://localhost:8000');
            await store.clear(); // should not throw
        });
    });
});
