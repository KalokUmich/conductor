/**
 * Ticket Provider — generic interface for ticket system integration.
 *
 * Designed to work with Jira first, extensible to Linear, GitHub Issues,
 * Azure DevOps, etc. Each provider implements the same interface.
 *
 * @module services/ticketProvider
 */

import type { JiraTokenStore } from './jiraTokenStore';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** Minimal ticket status for TODO sync. */
export interface TicketStatus {
    key: string;
    summary: string;
    status: string;         // e.g. "To Do", "In Progress", "Done"
    isDone: boolean;
    browseUrl: string;
}

/** Result of a batch status fetch. */
export interface TicketStatusResult {
    /** Successfully fetched statuses, keyed by ticket key. */
    statuses: Map<string, TicketStatus>;
    /** Whether the provider has valid auth. */
    authenticated: boolean;
}

// ---------------------------------------------------------------------------
// Interface
// ---------------------------------------------------------------------------

/** A ticket item from the provider (for Load Jira / backlog). */
export interface TicketItem {
    key: string;
    summary: string;
    status: string;
    priority: string;
    issuetype: string;
    assignee: string;
    isDone: boolean;
    browseUrl: string;
    /** Source identifier for UI badges. */
    source: 'ticket';
}

/**
 * Abstract ticket provider — implement for each ticketing system.
 */
export interface ITicketProvider {
    /** Provider name for display (e.g. "Jira", "Linear"). */
    readonly name: string;

    /** Tag prefix for code annotations, e.g. "jira" → {jira:DEV-123}. */
    readonly tagPrefix: string;

    /** Regex to detect ticket keys in text (e.g. /[A-Z]+-\d+/ for Jira). */
    readonly ticketKeyPattern: RegExp;

    /** Check if the provider is authenticated and ready to use. */
    isAuthenticated(): Promise<boolean>;

    /** Fetch status for multiple ticket keys in one call. */
    fetchStatuses(keys: string[]): Promise<TicketStatusResult>;

    /** Fetch tickets assigned to the current user. */
    fetchMyTickets(): Promise<TicketItem[]>;
}

// ---------------------------------------------------------------------------
// Jira Implementation
// ---------------------------------------------------------------------------

/** Jira ticket key pattern: PROJECT-123 */
const JIRA_KEY_PATTERN = /\b[A-Z][A-Z0-9]+-\d+\b/;

export class JiraTicketProvider implements ITicketProvider {
    readonly name = 'Jira';
    readonly tagPrefix = 'jira';
    readonly ticketKeyPattern = JIRA_KEY_PATTERN;

    constructor(
        private readonly _tokenStore: JiraTokenStore,
        private readonly _backendUrl: string,
    ) {}

    async isAuthenticated(): Promise<boolean> {
        // Check local tokens first (fast path)
        const tokens = await this._tokenStore.getValidTokens();
        if (tokens) return true;
        // Fall back to backend status (browser OAuth may have tokens we don't have locally)
        try {
            const resp = await fetch(`${this._backendUrl}/api/integrations/jira/status`);
            if (!resp.ok) return false;
            const status = await resp.json() as { connected: boolean };
            return status.connected;
        } catch {
            return false;
        }
    }

    async fetchStatuses(keys: string[]): Promise<TicketStatusResult> {
        if (keys.length === 0) {
            return { statuses: new Map(), authenticated: true };
        }

        // Check auth
        const tokens = await this._tokenStore.getValidTokens();
        if (!tokens) {
            return { statuses: new Map(), authenticated: false };
        }

        const statuses = new Map<string, TicketStatus>();

        // Batch fetch via JQL: key in (DEV-1, DEV-2, ...)
        try {
            const jql = `key in (${keys.join(',')})`;
            const resp = await fetch(
                `${this._backendUrl}/api/integrations/jira/search?${new URLSearchParams({
                    q: jql,
                    maxResults: String(keys.length),
                })}`,
            );

            if (!resp.ok) {
                // Fallback: try individual fetches
                return this._fetchIndividual(keys);
            }

            const issues = await resp.json() as Array<{
                key: string;
                summary: string;
                status: string;
                browse_url: string;
            }>;

            for (const issue of issues) {
                const isDone = /^(done|closed|resolved|complete)$/i.test(issue.status);
                statuses.set(issue.key, {
                    key: issue.key,
                    summary: issue.summary,
                    status: issue.status,
                    isDone,
                    browseUrl: issue.browse_url,
                });
            }

            return { statuses, authenticated: true };
        } catch {
            return { statuses, authenticated: true };
        }
    }

    async fetchMyTickets(): Promise<TicketItem[]> {
        try {
            const resp = await fetch(
                `${this._backendUrl}/api/integrations/jira/undone?maxResults=30`,
            );
            if (resp.status === 401) return []; // Not connected — caller handles auth
            if (!resp.ok) return [];

            const issues = await resp.json() as Array<{
                key: string;
                summary: string;
                status: string;
                priority: string;
                issuetype: string;
                assignee: string;
                browse_url: string;
            }>;

            return issues.map(issue => ({
                key: issue.key,
                summary: issue.summary,
                status: issue.status,
                priority: issue.priority,
                issuetype: issue.issuetype,
                assignee: issue.assignee,
                isDone: /^(done|closed|resolved|complete)$/i.test(issue.status),
                browseUrl: issue.browse_url,
                source: 'ticket' as const,
            }));
        } catch {
            return [];
        }
    }

    private async _fetchIndividual(keys: string[]): Promise<TicketStatusResult> {
        const statuses = new Map<string, TicketStatus>();

        for (const key of keys) {
            try {
                const resp = await fetch(
                    `${this._backendUrl}/api/integrations/jira/issue/${encodeURIComponent(key)}`,
                );
                if (!resp.ok) continue;

                const issue = await resp.json() as {
                    key: string;
                    summary: string;
                    status: string;
                    browse_url: string;
                };

                const isDone = /^(done|closed|resolved|complete)$/i.test(issue.status);
                statuses.set(issue.key, {
                    key: issue.key,
                    summary: issue.summary,
                    status: issue.status,
                    isDone,
                    browseUrl: issue.browse_url,
                });
            } catch {
                // Skip failures
            }
        }

        return { statuses, authenticated: true };
    }
}

// ---------------------------------------------------------------------------
// Utility: ticket tag formatting and parsing
// ---------------------------------------------------------------------------

/**
 * Tag regex: matches `{provider:KEY}` e.g. `{jira:DEV-123}`, `{github:#42}`.
 * Captures: (1) provider, (2) key.
 */
const TICKET_TAG_RE = /\{(\w+):([^}]+)\}/g;

/**
 * Format a ticket tag for embedding in code comments.
 * @example formatTicketTag('jira', 'DEV-123') → '{jira:DEV-123}'
 */
export function formatTicketTag(provider: string, key: string): string {
    return `{${provider}:${key}}`;
}

/**
 * Parse all ticket tags from text.
 * @returns Array of { provider, key } objects.
 */
export function parseTicketTags(text: string): Array<{ provider: string; key: string }> {
    const results: Array<{ provider: string; key: string }> = [];
    const re = new RegExp(TICKET_TAG_RE.source, 'g');
    let m: RegExpExecArray | null;
    while ((m = re.exec(text)) !== null) {
        results.push({ provider: m[1], key: m[2] });
    }
    return results;
}

/**
 * Extract all ticket keys from a string using the provider's pattern.
 * Also detects structured {provider:KEY} tags.
 */
export function extractTicketKeys(text: string, pattern: RegExp): string[] {
    const keys = new Set<string>();

    // Structured tags: {jira:DEV-123}
    for (const tag of parseTicketTags(text)) {
        keys.add(tag.key);
    }

    // Bare keys: DEV-123
    const globalPattern = new RegExp(pattern.source, 'g');
    const matches = text.match(globalPattern);
    if (matches) {
        for (const m of matches) keys.add(m);
    }

    return [...keys];
}
