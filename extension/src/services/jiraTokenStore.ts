/**
 * Jira Token Store — persistent token storage using VS Code SecretStorage.
 *
 * Tokens (access_token, refresh_token) are stored in the OS keychain via
 * VS Code's SecretStorage API.  Non-sensitive metadata (expires_at, cloud_id,
 * site_url) is stored in `.conductor/jira.json` for quick validity checks
 * without unlocking the keychain.
 *
 * The store also handles token refresh by calling the backend's
 * POST /api/integrations/jira/refresh endpoint, which combines the
 * client-side refresh_token with server-side client_id/client_secret.
 *
 * @module services/jiraTokenStore
 */

import type * as vscodeT from 'vscode';
import * as fs from 'fs/promises';
import * as path from 'path';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const SECRET_KEY_ACCESS = 'conductor.jira.accessToken';
const SECRET_KEY_REFRESH = 'conductor.jira.refreshToken';
const JIRA_META_FILE = 'jira.json';

/** Refresh 60 seconds before actual expiry (matches backend behavior). */
const REFRESH_MARGIN_MS = 60_000;

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** Metadata stored in `.conductor/jira.json`. */
export interface JiraTokenMeta {
    /** Unix timestamp (ms) when the access token expires. */
    expiresAt: number;
    /** Atlassian cloud ID. */
    cloudId: string;
    /** Jira site URL (e.g. https://mysite.atlassian.net). */
    siteUrl: string;
}

/** Full token set returned by the store. */
export interface JiraTokenSet {
    accessToken: string;
    refreshToken: string;
    meta: JiraTokenMeta;
}

// ---------------------------------------------------------------------------
// JiraTokenStore
// ---------------------------------------------------------------------------

export class JiraTokenStore {
    constructor(
        private readonly _secrets: vscodeT.SecretStorage,
        private readonly _workspaceRoot: string,
        private readonly _backendUrl: string,
    ) {}

    // -----------------------------------------------------------------------
    // Public API
    // -----------------------------------------------------------------------

    /**
     * Store a full token set after OAuth exchange or refresh.
     */
    async save(
        accessToken: string,
        refreshToken: string,
        expiresIn: number,
        cloudId: string,
        siteUrl: string,
    ): Promise<void> {
        await Promise.all([
            this._secrets.store(SECRET_KEY_ACCESS, accessToken),
            this._secrets.store(SECRET_KEY_REFRESH, refreshToken),
        ]);

        const meta: JiraTokenMeta = {
            expiresAt: Date.now() + expiresIn * 1000,
            cloudId,
            siteUrl,
        };
        await this._writeMeta(meta);
    }

    /**
     * Get a valid token set, refreshing if needed.
     *
     * Returns null if no tokens are stored or refresh fails.
     */
    async getValidTokens(): Promise<JiraTokenSet | null> {
        const meta = await this._readMeta();
        if (!meta) return null;

        const [accessToken, refreshToken] = await Promise.all([
            this._secrets.get(SECRET_KEY_ACCESS),
            this._secrets.get(SECRET_KEY_REFRESH),
        ]);

        if (!accessToken || !refreshToken) return null;

        // Check if access token is still valid (with margin)
        if (Date.now() < meta.expiresAt - REFRESH_MARGIN_MS) {
            return { accessToken, refreshToken, meta };
        }

        // Try to refresh
        return this._refresh(refreshToken, meta);
    }

    /**
     * Check if we have stored tokens (without validating expiry).
     * Quick check that avoids keychain access.
     */
    async hasTokens(): Promise<boolean> {
        const meta = await this._readMeta();
        return meta !== null;
    }

    /**
     * Read metadata only (no keychain access). Useful for status display.
     */
    async getMeta(): Promise<JiraTokenMeta | null> {
        return this._readMeta();
    }

    /**
     * Clear all stored tokens and metadata.
     */
    async clear(): Promise<void> {
        await Promise.all([
            this._secrets.delete(SECRET_KEY_ACCESS),
            this._secrets.delete(SECRET_KEY_REFRESH),
        ]);
        try {
            await fs.unlink(this._metaPath());
        } catch {
            // File may not exist
        }
    }

    // -----------------------------------------------------------------------
    // Token refresh
    // -----------------------------------------------------------------------

    private async _refresh(
        refreshToken: string,
        currentMeta: JiraTokenMeta,
    ): Promise<JiraTokenSet | null> {
        try {
            const resp = await fetch(`${this._backendUrl}/api/integrations/jira/refresh`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ refresh_token: refreshToken }),
            });

            if (!resp.ok) return null;

            const data = await resp.json() as {
                access_token: string;
                refresh_token: string;
                expires_in: number;
            };

            // Persist the new tokens
            await this.save(
                data.access_token,
                data.refresh_token,
                data.expires_in,
                currentMeta.cloudId,
                currentMeta.siteUrl,
            );

            return {
                accessToken: data.access_token,
                refreshToken: data.refresh_token,
                meta: {
                    expiresAt: Date.now() + data.expires_in * 1000,
                    cloudId: currentMeta.cloudId,
                    siteUrl: currentMeta.siteUrl,
                },
            };
        } catch {
            return null;
        }
    }

    // -----------------------------------------------------------------------
    // Metadata file I/O
    // -----------------------------------------------------------------------

    private _metaPath(): string {
        return path.join(this._workspaceRoot, '.conductor', JIRA_META_FILE);
    }

    private async _writeMeta(meta: JiraTokenMeta): Promise<void> {
        const dir = path.join(this._workspaceRoot, '.conductor');
        await fs.mkdir(dir, { recursive: true });
        await fs.writeFile(
            this._metaPath(),
            JSON.stringify(meta, null, 2) + '\n',
            'utf-8',
        );
    }

    private async _readMeta(): Promise<JiraTokenMeta | null> {
        try {
            const raw = await fs.readFile(this._metaPath(), 'utf-8');
            const parsed = JSON.parse(raw) as JiraTokenMeta;
            if (parsed.cloudId && parsed.siteUrl && typeof parsed.expiresAt === 'number') {
                return parsed;
            }
            return null;
        } catch {
            return null;
        }
    }
}
