/**
 * Conductor controller – orchestration layer between the FSM and side-effects.
 *
 * Owns a {@link ConductorStateMachine} and coordinates external checks
 * (e.g. backend health) before firing events on the FSM.
 *
 * All I/O is injected via constructor parameters so the controller is fully
 * unit-testable without real HTTP calls or VS Code APIs.
 *
 * @module services/conductorController
 */

import {
    ConductorEvent,
    ConductorState,
    ConductorStateMachine,
    StateChangeListener,
} from './conductorStateMachine';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/**
 * A function that checks whether the backend at the given URL is healthy.
 * Must return `true` when the backend is reachable and healthy, `false` otherwise.
 */
export type HealthCheckFn = (backendUrl: string) => Promise<boolean>;

/**
 * A function that returns the backend URL to health-check against.
 * Keeps the controller decoupled from VS Code configuration.
 */
export type UrlProviderFn = () => string;

/**
 * A function that resets the session and returns the new room ID.
 * Called when starting a new hosting session so every session gets a fresh ID.
 */
export type SessionResetFn = () => string;

/**
 * Data parsed from an invite URL.
 * Contains the information a guest needs to join a session.
 */
export interface ParsedInvite {
    /** The room ID extracted from the invite URL. */
    roomId: string;
    /** The backend URL (origin) extracted from the invite URL. */
    backendUrl: string;
    /** The Live Share URL extracted from the invite URL (if present). */
    liveShareUrl?: string;
}

// ---------------------------------------------------------------------------
// Controller
// ---------------------------------------------------------------------------

/**
 * Orchestrator that drives the {@link ConductorStateMachine} by performing
 * external checks (health check) and translating results into FSM events.
 *
 * Usage:
 * ```ts
 * const ctrl = new ConductorController(fsm, checkBackendHealth, () => getBackendUrl());
 * const newState = await ctrl.start();
 * ```
 */
export class ConductorController {
    private readonly _fsm: ConductorStateMachine;
    private readonly _healthCheck: HealthCheckFn;
    private readonly _urlProvider: UrlProviderFn;
    private readonly _sessionReset: SessionResetFn;

    /**
     * @param fsm          - The state machine to drive.
     * @param healthCheck  - Async function that pings the backend.
     * @param urlProvider  - Sync function that returns the backend URL.
     * @param sessionReset - Sync function that resets the session and returns a new roomId.
     */
    constructor(
        fsm: ConductorStateMachine,
        healthCheck: HealthCheckFn,
        urlProvider: UrlProviderFn,
        sessionReset: SessionResetFn = () => '',
    ) {
        this._fsm = fsm;
        this._healthCheck = healthCheck;
        this._urlProvider = urlProvider;
        this._sessionReset = sessionReset;
    }

    // ----- public API -------------------------------------------------------

    /**
     * Trigger the "start" flow from Idle.
     *
     * 1. Calls the health-check function against the configured backend URL.
     * 2. If the backend is healthy → fires {@link ConductorEvent.BACKEND_CONNECTED}.
     * 3. If the backend is unreachable → fires {@link ConductorEvent.BACKEND_LOST}.
     *
     * @returns The new {@link ConductorState} after the transition.
     * @throws {Error} If the FSM is not currently in the {@link ConductorState.Idle}
     *         or {@link ConductorState.BackendDisconnected} state.
     */
    async start(): Promise<ConductorState> {
        const current = this._fsm.getState();
        if (
            current !== ConductorState.Idle &&
            current !== ConductorState.BackendDisconnected
        ) {
            throw new Error(
                `Cannot start from state '${current}'. ` +
                `Expected '${ConductorState.Idle}' or '${ConductorState.BackendDisconnected}'.`,
            );
        }

        const backendUrl = this._urlProvider();
        const isHealthy = await this._healthCheck(backendUrl);

        if (isHealthy) {
            return this._fsm.transition(ConductorEvent.BACKEND_CONNECTED);
        } else {
            return this._fsm.transition(ConductorEvent.BACKEND_LOST);
        }
    }

    /**
     * Transition from ReadyToHost to Hosting.
     *
     * 1. Resets the session (generates a fresh roomId).
     * 2. Fires {@link ConductorEvent.START_HOSTING} on the FSM.
     *
     * @returns The new room ID for the hosting session.
     * @throws {Error} If the FSM is not in {@link ConductorState.ReadyToHost}.
     */
    startHosting(): string {
        const current = this._fsm.getState();
        if (current !== ConductorState.ReadyToHost) {
            throw new Error(
                `Cannot start hosting from state '${current}'. ` +
                `Expected '${ConductorState.ReadyToHost}'.`,
            );
        }

        const roomId = this._sessionReset();
        this._fsm.transition(ConductorEvent.START_HOSTING);
        return roomId;
    }

    /**
     * Transition from Hosting back to ReadyToHost.
     *
     * Fires {@link ConductorEvent.STOP_HOSTING} on the FSM.
     *
     * @throws {Error} If the FSM is not in {@link ConductorState.Hosting}.
     */
    stopHosting(): void {
        const current = this._fsm.getState();
        if (current !== ConductorState.Hosting) {
            throw new Error(
                `Cannot stop hosting from state '${current}'. ` +
                `Expected '${ConductorState.Hosting}'.`,
            );
        }

        this._fsm.transition(ConductorEvent.STOP_HOSTING);
    }

    /**
     * Parse an invite URL and transition from ReadyToHost to Joining.
     *
     * Expected URL format:
     *   `{backendUrl}/invite?roomId={roomId}&liveShareUrl={encodedLiveShareUrl}`
     *
     * @param inviteUrl - The full invite URL to parse.
     * @returns Parsed invite data (roomId, backendUrl, optional liveShareUrl).
     * @throws {Error} If the FSM is not in {@link ConductorState.ReadyToHost}.
     * @throws {Error} If the invite URL is malformed or missing required fields.
     */
    startJoining(inviteUrl: string): ParsedInvite {
        const current = this._fsm.getState();
        if (current !== ConductorState.ReadyToHost) {
            throw new Error(
                `Cannot join session from state '${current}'. ` +
                `Expected '${ConductorState.ReadyToHost}'.`,
            );
        }

        const parsed = ConductorController._parseInviteUrl(inviteUrl);
        this._fsm.transition(ConductorEvent.JOIN_SESSION);
        return parsed;
    }

    /**
     * Transition from Joining to Joined after successfully opening Live Share.
     *
     * @throws {Error} If the FSM is not in {@link ConductorState.Joining}.
     */
    joinSucceeded(): void {
        const current = this._fsm.getState();
        if (current !== ConductorState.Joining) {
            throw new Error(
                `Cannot mark join as succeeded from state '${current}'. ` +
                `Expected '${ConductorState.Joining}'.`,
            );
        }
        this._fsm.transition(ConductorEvent.JOIN_SUCCEEDED);
    }

    /**
     * Transition from Joining back to ReadyToHost on failure.
     *
     * @throws {Error} If the FSM is not in {@link ConductorState.Joining}.
     */
    joinFailed(): void {
        const current = this._fsm.getState();
        if (current !== ConductorState.Joining) {
            throw new Error(
                `Cannot mark join as failed from state '${current}'. ` +
                `Expected '${ConductorState.Joining}'.`,
            );
        }
        this._fsm.transition(ConductorEvent.JOIN_FAILED);
    }

    /**
     * Transition from Joined back to ReadyToHost.
     *
     * @throws {Error} If the FSM is not in {@link ConductorState.Joined}.
     */
    leaveSession(): void {
        const current = this._fsm.getState();
        if (current !== ConductorState.Joined) {
            throw new Error(
                `Cannot leave session from state '${current}'. ` +
                `Expected '${ConductorState.Joined}'.`,
            );
        }
        this._fsm.transition(ConductorEvent.LEAVE_SESSION);
    }

    /** Current FSM state (read-only convenience). */
    getState(): ConductorState {
        return this._fsm.getState();
    }

    /** Check whether a given event can be applied in the current state. */
    canTransition(event: ConductorEvent): boolean {
        return this._fsm.canTransition(event);
    }

    /** Subscribe to FSM state changes. Returns a dispose function. */
    onStateChange(listener: StateChangeListener): () => void {
        return this._fsm.onStateChange(listener);
    }

    // ----- private helpers --------------------------------------------------

    /**
     * Parse an invite URL into its constituent parts.
     *
     * @param inviteUrl - The invite URL string to parse.
     * @returns Parsed invite data.
     * @throws {Error} If the URL is malformed or missing a roomId query param.
     */
    private static _parseInviteUrl(inviteUrl: string): ParsedInvite {
        let url: URL;
        try {
            url = new URL(inviteUrl.trim());
        } catch {
            throw new Error(`Invalid invite URL: '${inviteUrl}'`);
        }

        const roomId = url.searchParams.get('roomId');
        if (!roomId) {
            throw new Error(
                `Invite URL is missing the 'roomId' query parameter: '${inviteUrl}'`,
            );
        }

        const backendUrl = `${url.protocol}//${url.host}`;
        const rawLiveShare = url.searchParams.get('liveShareUrl');
        const liveShareUrl = rawLiveShare
            ? decodeURIComponent(rawLiveShare)
            : undefined;

        return { roomId, backendUrl, liveShareUrl };
    }
}

