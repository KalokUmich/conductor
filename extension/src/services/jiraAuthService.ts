/**
 * Jira Connection Cache — validation, expiry logic, and URI handler.
 *
 * Stores Jira connection info with a timestamp so it can auto-expire after 48 hours.
 * Used by the extension host to persist/retrieve Jira connection from globalState,
 * and by the WebView to decide whether a cached connection is still valid.
 */

import type * as vscodeT from 'vscode';

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
 * URI handler for the Jira OAuth callback.
 *
 * Listens for `vscode://publisher.conductor/jira/callback` URIs and exchanges
 * the authorization code with the backend, or refreshes connection status.
 */
export class JiraUriHandler implements vscodeT.UriHandler {
    constructor(
        private readonly _onConnected: (status: { cloudId: string; siteUrl: string }) => void,
        private readonly _backendUrl: string,
    ) {}

    async handleUri(uri: vscodeT.Uri): Promise<void> {
        if (uri.path !== '/jira/callback') return;

        const params = new URLSearchParams(uri.query);
        const code = params.get('code');
        const connected = params.get('connected');

        if (connected === 'true') {
            // Backend already exchanged the code; just refresh status
            try {
                const resp = await fetch(`${this._backendUrl}/api/integrations/jira/status`);
                const status = await resp.json();
                if (status.connected) {
                    this._onConnected({ cloudId: status.cloud_id, siteUrl: status.site_url });
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
                const result = await resp.json();
                if (result.status === 'connected') {
                    this._onConnected({ cloudId: result.cloud_id, siteUrl: result.site_url });
                }
            } catch (e) {
                // Silently fail
            }
        }
    }
}
