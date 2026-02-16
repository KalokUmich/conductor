/**
 * SSO Identity Cache — validation and expiry logic.
 *
 * Stores SSO identity with a timestamp so it can auto-expire after 24 hours.
 * Used by the extension host to persist/retrieve SSO identity from globalState,
 * and by the WebView to decide whether a cached identity is still valid.
 */

/** How long an SSO identity is valid (24 hours). */
export const SSO_EXPIRY_MS = 24 * 60 * 60 * 1000;

/** Shape of the wrapper object stored in globalState. */
export interface SSOIdentityWrapper {
    identity: Record<string, unknown>;
    storedAt: number;
}

/**
 * Wrap a raw identity object with a timestamp for storage.
 */
export function wrapIdentity(identity: Record<string, unknown>, now: number = Date.now()): SSOIdentityWrapper {
    return { identity, storedAt: now };
}

/**
 * Extract a valid (non-expired) identity from a stored value.
 *
 * Handles three cases:
 *  - New format `{ identity, storedAt }` — returns identity if within 24h, null otherwise.
 *  - Old format (raw identity without storedAt) — treated as expired, returns null.
 *  - Falsy / missing — returns null.
 *
 * @param stored  The raw value read from globalState (unknown shape).
 * @param now     Current timestamp (injectable for testing).
 * @returns       The identity object if valid, or null.
 */
export function getValidIdentity(stored: unknown, now: number = Date.now()): Record<string, unknown> | null {
    if (!stored || typeof stored !== 'object') {
        return null;
    }

    const obj = stored as Record<string, unknown>;

    if ('storedAt' in obj && 'identity' in obj && typeof obj.storedAt === 'number') {
        const age = now - obj.storedAt;
        if (age < SSO_EXPIRY_MS) {
            return obj.identity as Record<string, unknown>;
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
export function isStale(stored: unknown, now: number = Date.now()): boolean {
    if (!stored || typeof stored !== 'object') {
        return false; // nothing to clear
    }
    return getValidIdentity(stored, now) === null;
}
