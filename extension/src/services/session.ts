/**
 * Session Management Service for Conductor Extension
 *
 * This module manages the collaboration session lifecycle, including:
 * - Room ID generation and persistence
 * - Host/User identification
 * - Live Share URL storage
 * - Session state across VS Code reloads
 *
 * The session is persisted using VS Code's globalState API, which stores
 * data per-workspace and survives VS Code restarts.
 *
 * @module services/session
 */
import * as vscode from 'vscode';
import { randomUUID } from 'crypto';

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
    /** Live Share URL for joining the session (optional). */
    liveShareUrl?: string;
}

/**
 * Singleton service for managing session and room lifecycle.
 *
 * Responsibilities:
 * - Generate and persist roomId using VS Code globalState
 * - Track host/user identification
 * - Store Live Share URL for invite generation
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
    /** Live Share URL (set when host starts session). */
    private _liveShareUrl: string | null = null;
    /** Cached ngrok URL (detected at startup). */
    private _ngrokUrl: string | null = null;

    // GlobalState persistence keys
    private static readonly ROOM_ID_KEY = 'aiCollab.roomId';
    private static readonly HOST_ID_KEY = 'aiCollab.hostId';
    private static readonly USER_ID_KEY = 'aiCollab.userId';
    private static readonly CREATED_AT_KEY = 'aiCollab.createdAt';
    private static readonly LIVE_SHARE_URL_KEY = 'aiCollab.liveShareUrl';

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
     * Detect and cache ngrok URL if ngrok is running.
     * Ngrok exposes its API at http://localhost:4040/api/tunnels
     *
     * @returns The ngrok public URL if available, null otherwise
     */
    public async detectNgrokUrl(): Promise<string | null> {
        try {
            // Use dynamic import for node-fetch or use built-in fetch
            const response = await fetch('http://localhost:4040/api/tunnels', {
                method: 'GET',
                headers: { 'Accept': 'application/json' }
            });

            if (!response.ok) {
                console.log('[Session] Ngrok API not available');
                return null;
            }

            const data = await response.json() as {
                tunnels: Array<{
                    public_url: string;
                    proto: string;
                    config: { addr: string };
                }>;
            };

            // Find the HTTPS tunnel that points to our backend port (8000)
            const tunnel = data.tunnels.find(t =>
                t.proto === 'https' && t.config.addr.includes('8000')
            );

            if (tunnel) {
                this._ngrokUrl = tunnel.public_url;
                console.log('[Session] Detected ngrok URL:', this._ngrokUrl);
                return this._ngrokUrl;
            }

            // If no specific tunnel found, try to get any HTTPS tunnel
            const httpsTunnel = data.tunnels.find(t => t.proto === 'https');
            if (httpsTunnel) {
                this._ngrokUrl = httpsTunnel.public_url;
                console.log('[Session] Detected ngrok URL (fallback):', this._ngrokUrl);
                return this._ngrokUrl;
            }

            console.log('[Session] No suitable ngrok tunnel found');
            return null;
        } catch (error) {
            // Ngrok is not running or API is not accessible
            console.log('[Session] Ngrok not detected:', error);
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
     * Set the Live Share URL for this session.
     */
    public setLiveShareUrl(url: string): void {
        this._liveShareUrl = url;
        if (this._context) {
            this._context.globalState.update(SessionService.LIVE_SHARE_URL_KEY, url);
        }
    }

    /**
     * Get the Live Share URL for this session.
     */
    public getLiveShareUrl(): string | null {
        return this._liveShareUrl;
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
            liveShareUrl: this._liveShareUrl || undefined
        };
    }

    /**
     * Generate the invite URL for guests.
     */
    public getInviteUrl(): string | null {
        if (!this._liveShareUrl) {
            return null;
        }
        const backendUrl = this.getBackendUrl();
        const roomId = this.getRoomId();
        const encodedLiveShareUrl = encodeURIComponent(this._liveShareUrl);
        return `${backendUrl}/invite?roomId=${roomId}&liveShareUrl=${encodedLiveShareUrl}`;
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
     * Overwrites roomId and Live Share URL with values from the invite.
     * The backendUrl is stored as the ngrok URL override so that
     * {@link getBackendUrl} returns the invite origin.
     *
     * @param roomId       - Room ID from the invite link.
     * @param backendUrl   - Backend origin from the invite link.
     * @param liveShareUrl - Live Share URL from the invite link (optional).
     */
    public joinAsGuest(
        roomId: string,
        backendUrl: string,
        liveShareUrl?: string,
    ): void {
        if (!this._context) {
            throw new Error('SessionService not initialized. Call initialize() first.');
        }

        this._roomId = roomId;
        this._ngrokUrl = backendUrl; // so getBackendUrl() returns the invite origin
        this._liveShareUrl = liveShareUrl ?? null;

        // Persist to globalState
        this._context.globalState.update(SessionService.ROOM_ID_KEY, this._roomId);
        if (liveShareUrl) {
            this._context.globalState.update(SessionService.LIVE_SHARE_URL_KEY, liveShareUrl);
        }

        console.log(
            `[SessionService] Joined as guest: roomId=${roomId}, backendUrl=${backendUrl}`,
        );
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
        this._liveShareUrl = null;

        // Persist to globalState
        this._context.globalState.update(SessionService.ROOM_ID_KEY, this._roomId);
        this._context.globalState.update(SessionService.HOST_ID_KEY, this._hostId);
        this._context.globalState.update(SessionService.USER_ID_KEY, this._userId);
        this._context.globalState.update(SessionService.CREATED_AT_KEY, this._createdAt);
        this._context.globalState.update(SessionService.LIVE_SHARE_URL_KEY, null);

        console.log(`[SessionService] Reset session: new roomId=${this._roomId}`);
    }
}

/**
 * Convenience function to get the session service instance.
 */
export function getSessionService(): SessionService {
    return SessionService.getInstance();
}

