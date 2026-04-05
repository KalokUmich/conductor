/**
 * ConductorController – orchestrates the Conductor extension lifecycle.
 *
 * Responsibilities
 * ----------------
 * - Run a one-shot health check on `start()` to determine the initial FSM state.
 * - Expose session lifecycle methods (startHosting, stopHosting, startJoining,
 *   joinSucceeded, joinFailed, leaveSession) that drive FSM transitions.
 * - Forward state-change notifications from the FSM to external listeners.
 *
 * Design principles
 * -----------------
 * - All dependencies are injected through the constructor (no static imports of
 *   VS Code APIs) so the class can be unit-tested without an extension host.
 * - The FSM is owned externally; the controller drives it but does not create it.
 *
 * @module services/conductorController
 */

import {
    ConductorStateMachine,
    ConductorState,
    ConductorEvent,
    StateChangeCallback,
} from './conductorStateMachine';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** Signature for the health-check function injected into the controller. */
export type HealthCheckFn = (url: string) => Promise<boolean>;

/** Parsed result returned by {@link ConductorController.startJoining}. */
export interface ParsedInvite {
    roomId: string;
    backendUrl: string;
}

// ---------------------------------------------------------------------------
// ConductorController
// ---------------------------------------------------------------------------

export class ConductorController {
    private readonly _fsm: ConductorStateMachine;
    private readonly _healthCheck: HealthCheckFn;
    private readonly _urlProvider: () => string;

    /**
     * @param fsm             - Externally owned state machine instance.
     * @param healthCheck     - Async function that checks backend reachability.
     * @param urlProvider     - Called each time the backend URL is needed.
     */
    constructor(
        fsm: ConductorStateMachine,
        healthCheck: HealthCheckFn,
        urlProvider: () => string,
    ) {
        this._fsm          = fsm;
        this._healthCheck  = healthCheck;
        this._urlProvider  = urlProvider;
    }

    // -----------------------------------------------------------------------
    // State access
    // -----------------------------------------------------------------------

    /** Current FSM state. */
    getState(): ConductorState {
        return this._fsm.getState();
    }

    /**
     * Returns true when `event` is a valid transition from the current state.
     * Does not mutate any state.
     */
    canTransition(event: ConductorEvent): boolean {
        return this._fsm.canTransition(event);
    }

    /**
     * Register a listener that is called after every FSM transition.
     *
     * @returns A dispose function that removes the listener.
     */
    onStateChange(cb: StateChangeCallback): () => void {
        return this._fsm.onStateChange(cb);
    }

    // -----------------------------------------------------------------------
    // Lifecycle
    // -----------------------------------------------------------------------

    /**
     * Perform an initial health check and transition the FSM accordingly.
     *
     * - If the backend is reachable → BACKEND_CONNECTED (Idle/BackendDisconnected → ReadyToHost).
     * - If the backend is unreachable → BACKEND_LOST (Idle → BackendDisconnected).
     *
     * Must only be called when the FSM is in Idle or BackendDisconnected.
     *
     * @throws {Error} When the FSM is in any other state.
     */
    async start(): Promise<ConductorState> {
        const current = this._fsm.getState();
        if (current !== ConductorState.Idle && current !== ConductorState.BackendDisconnected) {
            throw new Error(`Cannot start from state: ${current}`);
        }

        const alive = await this._healthCheck(this._urlProvider());

        if (alive) {
            this._fsm.transition(ConductorEvent.BACKEND_CONNECTED);
        } else {
            this._fsm.transition(ConductorEvent.BACKEND_LOST);
        }

        return this._fsm.getState();
    }

    // -----------------------------------------------------------------------
    // Hosting
    // -----------------------------------------------------------------------

    /**
     * Transition from ReadyToHost → Hosting.
     *
     * Calls `sessionResetFn` (if provided) to generate a fresh room ID.
     *
     * @returns The room ID produced by `sessionResetFn`, or an empty string if
     *          no reset function was provided.
     * @throws {Error} When the FSM is not in ReadyToHost.
     */
    /**
     * Transition to Hosting state. FSM-only — does NOT manage roomId.
     *
     * Caller must set roomId before calling this:
     *   - New session:  sessionService.resetSession()  → new roomId
     *   - Rejoin:       sessionService.setRoomId(old)  → old roomId
     *
     * @throws {Error} When the FSM is not in ReadyToHost.
     */
    startHosting(): void {
        const current = this._fsm.getState();
        if (current !== ConductorState.ReadyToHost) {
            throw new Error(`Cannot start hosting from state: ${current}`);
        }
        this._fsm.transition(ConductorEvent.START_HOSTING);
    }

    /**
     * Transition from Hosting → ReadyToHost.
     *
     * @throws {Error} When the FSM is not in Hosting.
     */
    stopHosting(): void {
        const current = this._fsm.getState();
        if (current !== ConductorState.Hosting) {
            throw new Error(`Cannot stop hosting from state: ${current}`);
        }
        this._fsm.transition(ConductorEvent.STOP_HOSTING);
    }

    // -----------------------------------------------------------------------
    // Joining
    // -----------------------------------------------------------------------

    /**
     * Parse an invite URL, transition the FSM to Joining, and return the
     * parsed invite details.
     *
     * Allowed starting states: ReadyToHost, BackendDisconnected.
     *
     * @param inviteUrl - The full invite URL (e.g. from a shared link).
     * @throws {Error} When the URL is invalid, `roomId` param is missing,
     *                 or the FSM is not in an allowed state.
     */
    startJoining(inviteUrl: string): ParsedInvite {
        const current = this._fsm.getState();
        if (
            current !== ConductorState.ReadyToHost &&
            current !== ConductorState.BackendDisconnected
        ) {
            throw new Error(`Cannot join session from state: ${current}`);
        }

        // Parse and validate the URL before touching the FSM.
        let parsed: URL;
        try {
            parsed = new URL(inviteUrl);
        } catch {
            throw new Error(`Invalid invite URL: "${inviteUrl}"`);
        }

        const roomId = parsed.searchParams.get('roomId');
        if (!roomId) {
            throw new Error(`Invite URL is missing the 'roomId' query parameter: "${inviteUrl}"`);
        }

        const backendUrl = parsed.origin;

        // URL is valid — now drive the FSM.
        this._fsm.transition(ConductorEvent.JOIN_SESSION);

        return { roomId, backendUrl };
    }

    /**
     * Transition from Joining → Joined.
     *
     * @throws {Error} When the FSM is not in Joining.
     */
    joinSucceeded(): void {
        const current = this._fsm.getState();
        if (current !== ConductorState.Joining) {
            throw new Error(`Cannot mark join as succeeded from state: ${current}`);
        }
        this._fsm.transition(ConductorEvent.JOIN_SUCCEEDED);
    }

    /**
     * Transition from Joining → ReadyToHost.
     *
     * @throws {Error} When the FSM is not in Joining.
     */
    joinFailed(): void {
        const current = this._fsm.getState();
        if (current !== ConductorState.Joining) {
            throw new Error(`Cannot mark join as failed from state: ${current}`);
        }
        this._fsm.transition(ConductorEvent.JOIN_FAILED);
    }

    /**
     * Transition from Joined → ReadyToHost.
     *
     * @throws {Error} When the FSM is not in Joined.
     */
    leaveSession(): void {
        const current = this._fsm.getState();
        if (current !== ConductorState.Joined) {
            throw new Error(`Cannot leave session from state: ${current}`);
        }
        this._fsm.transition(ConductorEvent.LEAVE_SESSION);
    }

    /**
     * Quit the session (leave but preserve data for later rejoin).
     *
     * Valid from Hosting or Joined states.  Unlike leaveSession / stopHosting,
     * the caller should NOT reset the roomId — it must be preserved so the
     * user can rejoin the same room later.
     *
     * @throws {Error} When the FSM is not in Hosting or Joined.
     */
    quitSession(): void {
        const current = this._fsm.getState();
        if (current !== ConductorState.Hosting && current !== ConductorState.Joined) {
            throw new Error(`Cannot quit session from state: ${current}`);
        }
        this._fsm.transition(ConductorEvent.QUIT_SESSION);
    }
}
