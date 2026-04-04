/**
 * Local session manager for tracking local-mode chat sessions.
 *
 * Stores session metadata in {globalStorageUri}/local_sessions.json
 * so users can see their session history, rejoin old sessions, or
 * clean up stale ones.
 *
 * Each session tracks: roomId, workspace path, creation time, last
 * active time, message count, and optional display name.
 *
 * @module services/localSessionManager
 */

import * as fs from 'fs';
import * as path from 'path';
import { getSessionsFilePath } from './conductorPaths';

export interface LocalSession {
    roomId: string;
    workspacePath: string;
    workspaceName: string;        // basename of workspace path
    displayName: string;          // session name (auto-generated or user-set)
    ssoEmail: string;
    createdAt: string;            // ISO 8601
    lastActiveAt: string;         // ISO 8601
    messageCount: number;
    mode: 'local' | 'online';    // session type
}

const STALE_DAYS = 7;

export class LocalSessionManager {
    private readonly filePath: string;
    private sessions: LocalSession[] = [];

    /**
     * @param _globalStoragePath - Legacy param (ignored). Storage is now at ~/.conductor/sessions.json.
     */
    constructor(_globalStoragePath?: string) {
        this.filePath = getSessionsFilePath();
        this.load();
    }

    /** Load sessions from disk. */
    private load(): void {
        try {
            if (fs.existsSync(this.filePath)) {
                const raw = fs.readFileSync(this.filePath, 'utf8');
                this.sessions = JSON.parse(raw);
            }
        } catch {
            this.sessions = [];
        }
    }

    /** Save sessions to disk. */
    private save(): void {
        try {
            const dir = path.dirname(this.filePath);
            if (!fs.existsSync(dir)) {
                fs.mkdirSync(dir, { recursive: true });
            }
            fs.writeFileSync(this.filePath, JSON.stringify(this.sessions, null, 2));
        } catch (e) {
            console.error('[LocalSessionManager] Failed to save:', e);
        }
    }

    /** Get all sessions for a specific SSO email. */
    getSessionsForUser(ssoEmail: string): LocalSession[] {
        return this.sessions
            .filter(s => s.ssoEmail === ssoEmail)
            .sort((a, b) => new Date(b.lastActiveAt).getTime() - new Date(a.lastActiveAt).getTime());
    }

    /** Get all sessions (regardless of user). */
    getAllSessions(): LocalSession[] {
        return [...this.sessions].sort(
            (a, b) => new Date(b.lastActiveAt).getTime() - new Date(a.lastActiveAt).getTime()
        );
    }

    /** Create or update a session when entering a room. */
    upsertSession(roomId: string, workspacePath: string, ssoEmail: string, mode: 'local' | 'online' = 'local'): LocalSession {
        const existing = this.sessions.find(s => s.roomId === roomId);
        const now = new Date().toISOString();

        if (existing) {
            existing.lastActiveAt = now;
            existing.ssoEmail = ssoEmail || existing.ssoEmail;
            this.save();
            return existing;
        }

        const workspaceName = path.basename(workspacePath) || workspacePath;
        const session: LocalSession = {
            roomId,
            workspacePath,
            workspaceName,
            displayName: `Session ${new Date().toLocaleDateString()} ${new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`,
            ssoEmail: ssoEmail || '',
            createdAt: now,
            lastActiveAt: now,
            messageCount: 0,
            mode,
        };
        this.sessions.push(session);
        this.save();
        return session;
    }

    /** Update session's last active time and message count. */
    touch(roomId: string, messageCount?: number): void {
        const session = this.sessions.find(s => s.roomId === roomId);
        if (session) {
            session.lastActiveAt = new Date().toISOString();
            if (messageCount !== undefined) {
                session.messageCount = messageCount;
            }
            this.save();
        }
    }

    /** Update session display name. */
    rename(roomId: string, displayName: string): void {
        const session = this.sessions.find(s => s.roomId === roomId);
        if (session) {
            session.displayName = displayName;
            this.save();
        }
    }

    /** Delete a session and its associated local data. */
    deleteSession(roomId: string): void {
        this.sessions = this.sessions.filter(s => s.roomId !== roomId);
        this.save();
    }

    /** Get stale sessions (older than STALE_DAYS). */
    getStaleSessions(): LocalSession[] {
        const cutoff = Date.now() - STALE_DAYS * 24 * 60 * 60 * 1000;
        return this.sessions.filter(
            s => new Date(s.lastActiveAt).getTime() < cutoff
        );
    }

    /** Check if a session is stale. */
    isStale(session: LocalSession): boolean {
        const cutoff = Date.now() - STALE_DAYS * 24 * 60 * 60 * 1000;
        return new Date(session.lastActiveAt).getTime() < cutoff;
    }
}
