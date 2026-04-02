/**
 * Jira Connection Cache — validation, expiry logic, and URI handler.
 *
 * Stores Jira connection info with a timestamp so it can auto-expire after 48 hours.
 * Used by the extension host to persist/retrieve Jira connection from globalState,
 * and by the WebView to decide whether a cached connection is still valid.
 *
 * Token persistence: after OAuth exchange, tokens are saved to JiraTokenStore
 * (SecretStorage + .conductor/jira.json).  On subsequent sessions, the extension
 * checks local tokens first before requiring re-authentication.
 */

import type * as vscodeT from 'vscode';

import type { JiraTokenStore } from './jiraTokenStore';

/** How long a Jira connection is valid (48 hours). */
export const JIRA_EXPIRY_MS = 48 * 60 * 60 * 1000;

/** globalState key for persisted Jira connection. */
export const JIRA_GLOBALSTATE_KEY = 'conductor.jiraConnection';

/** Shape of the Jira connection info. */
export interface JiraConnectionInfo {
    connected: boolean;
    siteUrl: string;
    cloudId: string;
}

/** Shape of the wrapper object stored in globalState. */
export interface JiraConnectionWrapper {
    connection: JiraConnectionInfo;
    storedAt: number;
}

/**
 * Wrap a Jira connection object with a timestamp for storage.
 */
export function wrapJiraConnection(
    connection: JiraConnectionInfo,
    now: number = Date.now(),
): JiraConnectionWrapper {
    return { connection, storedAt: now };
}

/**
 * Extract a valid (non-expired) Jira connection from a stored value.
 *
 * Handles three cases:
 *  - New format `{ connection, storedAt }` — returns connection if within 48h, null otherwise.
 *  - Old format (raw connection without storedAt) — treated as expired, returns null.
 *  - Falsy / missing — returns null.
 *
 * @param stored  The raw value read from globalState (unknown shape).
 * @param now     Current timestamp (injectable for testing).
 * @returns       The connection object if valid, or null.
 */
export function getValidJiraConnection(stored: unknown, now: number = Date.now()): JiraConnectionInfo | null {
    if (!stored || typeof stored !== 'object') {
        return null;
    }

    const obj = stored as Record<string, unknown>;

    if ('storedAt' in obj && 'connection' in obj && typeof obj.storedAt === 'number') {
        const age = now - obj.storedAt;
        if (age < JIRA_EXPIRY_MS) {
            return obj.connection as JiraConnectionInfo;
        }
        // Expired
        return null;
    }

    // Old format (no storedAt) — treat as expired
    return null;
}

/**
 * Check whether a stored value is stale and should be cleared from globalState.
 * Returns true if the value exists but is expired or in old format.
 */
export function isJiraConnectionStale(stored: unknown, now: number = Date.now()): boolean {
    if (!stored || typeof stored !== 'object') {
        return false; // nothing to clear
    }
    return getValidJiraConnection(stored, now) === null;
}

/**
 * Restore Jira connection from locally-stored tokens.
 *
 * Called on extension activation — checks if the token store has valid
 * (or refreshable) tokens and restores the connection without re-auth.
 *
 * @returns The connection info if tokens are valid/refreshed, null otherwise.
 */
export async function restoreJiraFromTokenStore(
    tokenStore: JiraTokenStore,
): Promise<JiraConnectionInfo | null> {
    const tokenSet = await tokenStore.getValidTokens();
    if (!tokenSet) return null;

    return {
        connected: true,
        siteUrl: tokenSet.meta.siteUrl,
        cloudId: tokenSet.meta.cloudId,
    };
}

/**
 * URI handler for the Jira OAuth callback.
 *
 * Listens for `vscode://publisher.conductor/jira/callback` URIs and exchanges
 * the authorization code with the backend, or refreshes connection status.
 *
 * After successful auth, tokens are persisted to JiraTokenStore for future
 * sessions (avoids re-auth on extension reload / backend restart).
 */
export class JiraUriHandler implements vscodeT.UriHandler {
    constructor(
        private readonly _onConnected: (status: { cloudId: string; siteUrl: string }) => void,
        private readonly _backendUrl: string,
        private readonly _tokenStore?: JiraTokenStore,
    ) {}

    async handleUri(uri: vscodeT.Uri): Promise<void> {
        if (uri.path !== '/jira/callback') return;

        const params = new URLSearchParams(uri.query);
        const code = params.get('code');
        const connected = params.get('connected');

        if (connected === 'true') {
            // Backend already exchanged the code via browser flow.
            // Fetch tokens from backend so we can persist them locally.
            try {
                const resp = await fetch(`${this._backendUrl}/api/integrations/jira/tokens`);
                if (resp.ok) {
                    const data = await resp.json() as {
                        access_token: string;
                        refresh_token: string;
                        expires_in: number;
                        cloud_id: string;
                        site_url: string;
                    };
                    // Persist tokens locally
                    if (this._tokenStore) {
                        await this._tokenStore.save(
                            data.access_token,
                            data.refresh_token,
                            data.expires_in,
                            data.cloud_id,
                            data.site_url,
                        );
                    }
                    this._onConnected({ cloudId: data.cloud_id, siteUrl: data.site_url });
                } else {
                    // Fall back to status-only (no token persistence)
                    const statusResp = await fetch(`${this._backendUrl}/api/integrations/jira/status`);
                    const status = await statusResp.json() as { connected: boolean; cloud_id: string; site_url: string };
                    if (status.connected) {
                        this._onConnected({ cloudId: status.cloud_id, siteUrl: status.site_url });
                    }
                }
            } catch (e) {
                // Silently fail — user can manually check status
            }
            return;
        }

        if (code) {
            // Extension received the code directly — exchange with backend
            try {
                const resp = await fetch(`${this._backendUrl}/api/integrations/jira/callback`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ code, state: params.get('state') || '' }),
                });
                const result = await resp.json() as {
                    status: string;
                    cloud_id: string;
                    site_url: string;
                    access_token: string;
                    refresh_token: string;
                    expires_in: number;
                };
                if (result.status === 'connected') {
                    // Persist tokens locally
                    if (this._tokenStore) {
                        await this._tokenStore.save(
                            result.access_token,
                            result.refresh_token,
                            result.expires_in,
                            result.cloud_id,
                            result.site_url,
                        );
                    }
                    this._onConnected({ cloudId: result.cloud_id, siteUrl: result.site_url });
                }
            } catch (e) {
                // Silently fail
            }
        }
    }
}
