/**
 * Unified credential store for Conductor.
 *
 * All credentials (SSO identity, Jira tokens, future integrations) are stored
 * under ~/.conductor/credentials/ as JSON files. This replaces the scattered
 * storage across VS Code globalState, SecretStorage, and workspace files.
 *
 * Security: ~/.conductor/credentials/ is created with mode 0700.
 * Tokens are stored in plaintext (like Claude Code's .credentials.json).
 * OS-level file permissions are the primary protection.
 *
 * @module services/credentialStore
 */

import * as fs from 'fs';
import * as path from 'path';
import { getCredentialsDir } from './conductorPaths';

// ============================================================
// SSO Identity
// ============================================================

export interface SSOIdentity {
    email: string;
    name?: string;
    provider: string;
    arn?: string;
    userUuid?: string;     // Stable backend-assigned UUID
    storedAt: number;      // Unix timestamp (ms)
    expiresAt: number;     // Unix timestamp (ms)
}

const SSO_FILE = 'sso.json';
const SSO_EXPIRY_MS = 48 * 60 * 60 * 1000; // 48 hours

export function saveSSO(identity: Record<string, unknown>, provider: string, userUuid?: string): void {
    const data: SSOIdentity = {
        email: (identity.email as string) || (identity.arn as string) || '',
        name: (identity.name as string) || undefined,
        provider,
        arn: (identity.arn as string) || undefined,
        userUuid,
        storedAt: Date.now(),
        expiresAt: Date.now() + SSO_EXPIRY_MS,
    };
    const fp = path.join(getCredentialsDir(), SSO_FILE);
    fs.writeFileSync(fp, JSON.stringify(data, null, 2), { mode: 0o600 });
}

export function loadSSO(): SSOIdentity | null {
    const fp = path.join(getCredentialsDir(), SSO_FILE);
    try {
        if (!fs.existsSync(fp)) return null;
        const raw = fs.readFileSync(fp, 'utf8');
        const data: SSOIdentity = JSON.parse(raw);
        if (Date.now() > data.expiresAt) {
            // Expired — delete and return null
            fs.unlinkSync(fp);
            return null;
        }
        return data;
    } catch {
        return null;
    }
}

export function clearSSO(): void {
    const fp = path.join(getCredentialsDir(), SSO_FILE);
    try {
        if (fs.existsSync(fp)) fs.unlinkSync(fp);
    } catch { /* ignore */ }
}

// ============================================================
// Jira Tokens
// ============================================================

export interface JiraCredentials {
    accessToken: string;
    refreshToken: string;
    expiresAt: number;     // Unix timestamp (ms)
    cloudId: string;
    siteUrl: string;
}

const JIRA_FILE = 'jira.json';

export function saveJira(creds: JiraCredentials): void {
    const fp = path.join(getCredentialsDir(), JIRA_FILE);
    fs.writeFileSync(fp, JSON.stringify(creds, null, 2), { mode: 0o600 });
}

export function loadJira(): JiraCredentials | null {
    const fp = path.join(getCredentialsDir(), JIRA_FILE);
    try {
        if (!fs.existsSync(fp)) return null;
        const raw = fs.readFileSync(fp, 'utf8');
        return JSON.parse(raw) as JiraCredentials;
    } catch {
        return null;
    }
}

export function clearJira(): void {
    const fp = path.join(getCredentialsDir(), JIRA_FILE);
    try {
        if (fs.existsSync(fp)) fs.unlinkSync(fp);
    } catch { /* ignore */ }
}
