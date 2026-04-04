/**
 * Session Management Service for Conductor Extension
 *
 * This module manages the collaboration session lifecycle, including:
 * - Room ID generation and persistence
 * - Host/User identification
 * - Session state across VS Code reloads
 *
 * The session is persisted using VS Code's globalState API, which stores
 * data per-workspace and survives VS Code restarts.
 *
 * @module services/session
 */
import * as vscode from 'vscode';
import { randomUUID } from 'crypto';

import { NgrokTunnel, selectPreferredNgrokUrl } from './connectionDiagnostics';

/**
 * Session state data structure passed to the WebView.
 * Contains all information needed for the chat interface.
 */
export interface SessionState {
    /** Unique identifier for this collaboration room. */
    roomId: string;
    /** Machine ID of the host (for identifying the session owner). */
    hostId: string;
    /** Unique identifier for this user within the session. */
    userId: string;
    /** Unix timestamp (ms) when the session was created. */
    createdAt: number;
    /** Backend server URL for API calls. */
    backendUrl: string;
}

/**
 * Singleton service for managing session and room lifecycle.
 *
 * Responsibilities:
 * - Generate and persist roomId using VS Code globalState
 * - Track host/user identification
 * - Provide session state to WebView
 *
 * Usage:
 *   const session = getSessionService();
 *   session.initialize(context);
 *   const roomId = session.getRoomId();
 */
export class SessionService {
    /** Singleton instance. */
    private static instance: SessionService;

    /** VS Code extension context for globalState access. */
    private _context: vscode.ExtensionContext | null = null;

    /** Current room/session identifier. */
    private _roomId: string | null = null;
    /** Host machine ID (from vscode.env.machineId). */
    private _hostId: string | null = null;
    /** Current user's unique ID. */
    private _userId: string | null = null;
    /** Session creation timestamp (Unix ms). */
    private _createdAt: number | null = null;
    /** Cached ngrok URL (detected at startup). */
    private _ngrokUrl: string | null = null;

    // GlobalState persistence keys
    private static readonly ROOM_ID_KEY = 'aiCollab.roomId';
    private static readonly HOST_ID_KEY = 'aiCollab.hostId';
    private static readonly USER_ID_KEY = 'aiCollab.userId';
    private static readonly CREATED_AT_KEY = 'aiCollab.createdAt';
    /** Private constructor for singleton pattern. */
    private constructor() {}

    /**
     * Get the singleton instance of the SessionService.
     */
    public static getInstance(): SessionService {
        if (!SessionService.instance) {
            SessionService.instance = new SessionService();
        }
        return SessionService.instance;
    }

    /**
     * Initialize the session service with the extension context.
     * Must be called during extension activation.
     * 
     * @param context The VS Code extension context
     */
    public initialize(context: vscode.ExtensionContext): void {
        this._context = context;
        this._loadOrCreateSession();
    }

    /**
     * Load existing session from globalState or create a new one.
     */
    private _loadOrCreateSession(): void {
        if (!this._context) {
            throw new Error('SessionService not initialized. Call initialize() first.');
        }

        // Try to load existing session
        const existingRoomId = this._context.globalState.get<string>(SessionService.ROOM_ID_KEY);
        const existingHostId = this._context.globalState.get<string>(SessionService.HOST_ID_KEY);
        const existingUserId = this._context.globalState.get<string>(SessionService.USER_ID_KEY);
        const existingCreatedAt = this._context.globalState.get<number>(SessionService.CREATED_AT_KEY);

        if (existingRoomId && existingHostId && existingUserId && existingCreatedAt) {
            // Reuse existing session
            this._roomId = existingRoomId;
            this._hostId = existingHostId;
            this._userId = existingUserId;
            this._createdAt = existingCreatedAt;
            console.log(`[SessionService] Restored session: roomId=${this._roomId}, userId=${this._userId}`);
        } else {
            // Create new session
            this._roomId = randomUUID();
            this._hostId = vscode.env.machineId;
            this._userId = randomUUID();
            this._createdAt = Date.now();

            // Persist to globalState
            this._context.globalState.update(SessionService.ROOM_ID_KEY, this._roomId);
            this._context.globalState.update(SessionService.HOST_ID_KEY, this._hostId);
            this._context.globalState.update(SessionService.USER_ID_KEY, this._userId);
            this._context.globalState.update(SessionService.CREATED_AT_KEY, this._createdAt);

            console.log(`[SessionService] Created new session: roomId=${this._roomId}, userId=${this._userId}`);
        }
    }

    /**
     * Get the current room ID.
     * @throws Error if service is not initialized
     */
    public getRoomId(): string {
        if (!this._roomId) {
            throw new Error('SessionService not initialized. Call initialize() first.');
        }
        return this._roomId;
    }

    /**
     * Get the host ID (machine ID of the host).
     * @throws Error if service is not initialized
     */
    public getHostId(): string {
        if (!this._hostId) {
            throw new Error('SessionService not initialized. Call initialize() first.');
        }
        return this._hostId;
    }

    /**
     * Get the session creation timestamp.
     * @throws Error if service is not initialized
     */
    public getCreatedAt(): number {
        if (!this._createdAt) {
            throw new Error('SessionService not initialized. Call initialize() first.');
        }
        return this._createdAt;
    }

    /**
     * Get the user ID (unique identifier for this user).
     * @throws Error if service is not initialized
     */
    public getUserId(): string {
        if (!this._userId) {
            throw new Error('SessionService not initialized. Call initialize() first.');
        }
        return this._userId;
    }

    /**
     * Get the backend URL.
     * Priority: ngrok URL (if detected) > config setting > default localhost
     */
    public getBackendUrl(): string {
        // If ngrok URL was detected, use it
        if (this._ngrokUrl) {
            return this._ngrokUrl;
        }
        // Otherwise use config setting
        const config = vscode.workspace.getConfiguration('aiCollab');
        return config.get<string>('backendUrl', 'http://localhost:8000');
    }

    /**
     * Detect and cache the public URL to use for invite links.
     *
     * Priority:
     *   1. Backend GET /public-url  — reads conductor.settings.yaml server.public_url
     *   2. Local ngrok API on localhost:4040 (useful if ngrok runs inside WSL)
     *   3. Returns null → falls back to VS Code aiCollab.backendUrl / localhost
     *
     * @returns The public URL if available, null otherwise
     */
    public async detectNgrokUrl(): Promise<string | null> {
        // --- 1. Ask the backend for its configured public_url ---
        try {
            const localBackend = vscode.workspace.getConfiguration('aiCollab')
                .get<string>('backendUrl', 'http://localhost:8000');
            const resp = await fetch(`${localBackend}/public-url`, {
                method: 'GET',
                headers: { 'Accept': 'application/json' },
                signal: AbortSignal.timeout(3000),
            });
            if (resp.ok) {
                const data = await resp.json() as { public_url?: string };
                const url = (data.public_url ?? '').trim();
                if (url) {
                    this._ngrokUrl = url;
                    console.log('[Session] Using backend-configured public_url:', url);
                    return url;
                }
            }
        } catch {
            // backend not reachable yet — fall through to local ngrok detection
        }

        // --- 2. Fall back to local ngrok API ---
        try {
            const response = await fetch('http://localhost:4040/api/tunnels', {
                method: 'GET',
                headers: { 'Accept': 'application/json' },
                signal: AbortSignal.timeout(3000),
            });

            if (!response.ok) {
                console.log('[Session] Ngrok API unavailable on localhost:4040; using configured backend URL / localhost fallback');
                return null;
            }

            const data = await response.json() as {
                tunnels: Array<{
                    public_url: string;
                    proto: string;
                    config?: { addr?: string } | null;
                }>;
            };

            const detectedUrl = selectPreferredNgrokUrl(data.tunnels.map(tunnel => ({
                publicUrl: tunnel.public_url,
                proto: tunnel.proto,
                config: tunnel.config,
            } satisfies NgrokTunnel)));
            if (detectedUrl) {
                this._ngrokUrl = detectedUrl;
                console.log('[Session] Using detected ngrok tunnel:', this._ngrokUrl);
                return this._ngrokUrl;
            }

            console.log('[Session] No local ngrok HTTPS tunnel detected; using configured backend URL / localhost fallback');
            return null;
        } catch {
            console.log('[Session] No local ngrok tunnel detected; using configured backend URL / localhost fallback');
            return null;
        }
    }

    /**
     * Get the cached ngrok URL (if any).
     */
    public getNgrokUrl(): string | null {
        return this._ngrokUrl;
    }

    /**
     * Get session state to pass to the WebView.
     * This returns a serializable object that can be sent to the frontend.
     */
    public getSessionStateForWebView(): SessionState {
        return {
            roomId: this.getRoomId(),
            hostId: this.getHostId(),
            userId: this.getUserId(),
            createdAt: this.getCreatedAt(),
            backendUrl: this.getBackendUrl(),
        };
    }

    /**
     * Generate the invite URL for guests.
     */
    public getInviteUrl(): string {
        const backendUrl = this.getBackendUrl();
        const roomId = this.getRoomId();
        return `${backendUrl}/chat?roomId=${roomId}`;
    }

    /**
     * Check if the service has been initialized.
     */
    public isInitialized(): boolean {
        return this._context !== null && this._roomId !== null;
    }

    /**
     * Configure this session as a guest joining an existing room.
     *
     * Overwrites roomId with value from the invite.
     * The backendUrl is stored as the ngrok URL override so that
     * {@link getBackendUrl} returns the invite origin.
     *
     * @param roomId     - Room ID from the invite link.
     * @param backendUrl - Backend origin from the invite link.
     */
    public joinAsGuest(
        roomId: string,
        backendUrl: string,
    ): void {
        if (!this._context) {
            throw new Error('SessionService not initialized. Call initialize() first.');
        }

        this._roomId = roomId;
        this._ngrokUrl = backendUrl; // so getBackendUrl() returns the invite origin

        // Persist to globalState
        this._context.globalState.update(SessionService.ROOM_ID_KEY, this._roomId);

        console.log(
            `[SessionService] Joined as guest: roomId=${roomId}, backendUrl=${backendUrl}`,
        );
    }

    /**
     * Set roomId to a specific value (for session rejoin).
     */
    public setRoomId(roomId: string): void {
        if (!this._context) {
            throw new Error('SessionService not initialized.');
        }
        this._roomId = roomId;
        this._context.globalState.update(SessionService.ROOM_ID_KEY, roomId);
        console.log(`[SessionService] Room ID set to: ${roomId}`);
    }

    /**
     * Reset the session (create new roomId).
     * Used when host ends the chat session.
     */
    public resetSession(): void {
        if (!this._context) {
            throw new Error('SessionService not initialized. Call initialize() first.');
        }

        // Create new session
        this._roomId = randomUUID();
        this._hostId = vscode.env.machineId;
        this._userId = randomUUID();
        this._createdAt = Date.now();

        // Persist to globalState
        this._context.globalState.update(SessionService.ROOM_ID_KEY, this._roomId);
        this._context.globalState.update(SessionService.HOST_ID_KEY, this._hostId);
        this._context.globalState.update(SessionService.USER_ID_KEY, this._userId);
        this._context.globalState.update(SessionService.CREATED_AT_KEY, this._createdAt);

        console.log(`[SessionService] Reset session: new roomId=${this._roomId}`);
    }

    // ------------------------------------------------------------------
    // Quit room history (for rejoin)
    // ------------------------------------------------------------------

    private static readonly QUIT_ROOMS_KEY = 'conductor.quitRooms';
    private static readonly MAX_QUIT_ROOMS = 10;

    /** Save the current room as a "quit room" for later rejoin. */
    public saveQuitRoom(roomId: string, backendUrl: string): void {
        if (!this._context) { return; }
        const rooms: QuitRoom[] =
            this._context.globalState.get(SessionService.QUIT_ROOMS_KEY, []) as QuitRoom[];
        // Deduplicate
        const filtered = rooms.filter(r => r.roomId !== roomId);
        filtered.push({ roomId, backendUrl, quitAt: Date.now() });
        // Keep only the most recent N
        const trimmed = filtered.slice(-SessionService.MAX_QUIT_ROOMS);
        this._context.globalState.update(SessionService.QUIT_ROOMS_KEY, trimmed);
        console.log(`[SessionService] Saved quit room: ${roomId}`);
    }

    /** Get the list of rooms the user previously quit (for rejoin). */
    public getQuitRooms(): QuitRoom[] {
        if (!this._context) { return []; }
        return this._context.globalState.get(SessionService.QUIT_ROOMS_KEY, []) as QuitRoom[];
    }

    /** Remove a quit room from the list (e.g., after rejoin or delete). */
    public removeQuitRoom(roomId: string): void {
        if (!this._context) { return; }
        const rooms: QuitRoom[] =
            this._context.globalState.get(SessionService.QUIT_ROOMS_KEY, []) as QuitRoom[];
        const filtered = rooms.filter(r => r.roomId !== roomId);
        this._context.globalState.update(SessionService.QUIT_ROOMS_KEY, filtered);
    }
}

/** Persisted quit room entry for later rejoin. */
export interface QuitRoom {
    roomId: string;
    backendUrl: string;
    quitAt: number;
}

/**
 * Convenience function to get the session service instance.
 */
export function getSessionService(): SessionService {
    return SessionService.getInstance();
}

