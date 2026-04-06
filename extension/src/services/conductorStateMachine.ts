/**
 * Finite State Machine for the Conductor extension lifecycle.
 *
 * Manages transitions between extension states (Idle, BackendDisconnected,
 * ReadyToHost, CreatingWorkspace, Hosting, Joining, Joined) driven by
 * discrete events. Invalid transitions are rejected with an error.
 *
 * The module is intentionally free of VS Code API dependencies so that it
 * can be unit-tested without the extension host.
 *
 * @module services/conductorStateMachine
 */

// ---------------------------------------------------------------------------
// Enums
// ---------------------------------------------------------------------------

/** All possible states of the Conductor extension. */
export enum ConductorState {
    Idle = 'Idle',
    BackendDisconnected = 'BackendDisconnected',
    ReadyToHost = 'ReadyToHost',
    CreatingWorkspace = 'CreatingWorkspace',
    Hosting = 'Hosting',
    Joining = 'Joining',
    Joined = 'Joined',
}

/** Events that drive state transitions. */
export enum ConductorEvent {
    BACKEND_CONNECTED = 'BACKEND_CONNECTED',
    BACKEND_LOST = 'BACKEND_LOST',
    START_HOSTING = 'START_HOSTING',
    STOP_HOSTING = 'STOP_HOSTING',
    JOIN_SESSION = 'JOIN_SESSION',
    JOIN_SUCCEEDED = 'JOIN_SUCCEEDED',
    JOIN_FAILED = 'JOIN_FAILED',
    LEAVE_SESSION = 'LEAVE_SESSION',
    CREATE_WORKSPACE = 'CREATE_WORKSPACE',
    WORKSPACE_READY = 'WORKSPACE_READY',
    WORKSPACE_FAILED = 'WORKSPACE_FAILED',
    DESTROY_WORKSPACE = 'DESTROY_WORKSPACE',
    QUIT_SESSION = 'QUIT_SESSION',
}

// ---------------------------------------------------------------------------
// Error class
// ---------------------------------------------------------------------------

/**
 * Thrown when a transition is attempted that has no entry in the
 * {@link TRANSITION_TABLE} for the current state and event.
 */
export class InvalidTransitionError extends Error {
    constructor(
        public readonly from: ConductorState,
        public readonly event: ConductorEvent,
    ) {
        super(`Invalid transition: state=${from}, event=${event}`);
        this.name = 'InvalidTransitionError';
    }
}

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** Callback signature for state-change listeners. */
export type StateChangeCallback = (
    prev: ConductorState,
    next: ConductorState,
    event: ConductorEvent,
) => void;

// ---------------------------------------------------------------------------
// Transition table
// ---------------------------------------------------------------------------

/**
 * Lookup table that maps (currentState, event) → nextState.
 * Any pair not present in this table is considered an invalid transition.
 */
const TRANSITION_TABLE: Record<string, ConductorState> = {
    // From Idle
    [`${ConductorState.Idle}:${ConductorEvent.BACKEND_CONNECTED}`]: ConductorState.ReadyToHost,
    [`${ConductorState.Idle}:${ConductorEvent.BACKEND_LOST}`]: ConductorState.BackendDisconnected,

    // From BackendDisconnected
    [`${ConductorState.BackendDisconnected}:${ConductorEvent.BACKEND_CONNECTED}`]: ConductorState.ReadyToHost,
    [`${ConductorState.BackendDisconnected}:${ConductorEvent.JOIN_SESSION}`]: ConductorState.Joining,

    // From ReadyToHost
    [`${ConductorState.ReadyToHost}:${ConductorEvent.BACKEND_LOST}`]: ConductorState.BackendDisconnected,
    [`${ConductorState.ReadyToHost}:${ConductorEvent.START_HOSTING}`]: ConductorState.Hosting,
    [`${ConductorState.ReadyToHost}:${ConductorEvent.JOIN_SESSION}`]: ConductorState.Joining,
    [`${ConductorState.ReadyToHost}:${ConductorEvent.CREATE_WORKSPACE}`]: ConductorState.CreatingWorkspace,

    // From CreatingWorkspace
    [`${ConductorState.CreatingWorkspace}:${ConductorEvent.WORKSPACE_READY}`]: ConductorState.ReadyToHost,
    [`${ConductorState.CreatingWorkspace}:${ConductorEvent.WORKSPACE_FAILED}`]: ConductorState.ReadyToHost,
    [`${ConductorState.CreatingWorkspace}:${ConductorEvent.DESTROY_WORKSPACE}`]: ConductorState.ReadyToHost,
    [`${ConductorState.CreatingWorkspace}:${ConductorEvent.BACKEND_LOST}`]: ConductorState.BackendDisconnected,

    // From Hosting
    [`${ConductorState.Hosting}:${ConductorEvent.STOP_HOSTING}`]: ConductorState.ReadyToHost,
    [`${ConductorState.Hosting}:${ConductorEvent.QUIT_SESSION}`]: ConductorState.ReadyToHost,
    [`${ConductorState.Hosting}:${ConductorEvent.BACKEND_LOST}`]: ConductorState.BackendDisconnected,

    // From Joining
    [`${ConductorState.Joining}:${ConductorEvent.JOIN_SUCCEEDED}`]: ConductorState.Joined,
    [`${ConductorState.Joining}:${ConductorEvent.JOIN_FAILED}`]: ConductorState.ReadyToHost,
    [`${ConductorState.Joining}:${ConductorEvent.BACKEND_LOST}`]: ConductorState.BackendDisconnected,

    // From Joined
    [`${ConductorState.Joined}:${ConductorEvent.LEAVE_SESSION}`]: ConductorState.ReadyToHost,
    [`${ConductorState.Joined}:${ConductorEvent.QUIT_SESSION}`]: ConductorState.ReadyToHost,
    [`${ConductorState.Joined}:${ConductorEvent.BACKEND_LOST}`]: ConductorState.BackendDisconnected,
    [`${ConductorState.Joined}:${ConductorEvent.START_HOSTING}`]: ConductorState.Hosting,
};

// ---------------------------------------------------------------------------
// State machine implementation
// ---------------------------------------------------------------------------

/**
 * Immutable snapshot returned by {@link ConductorStateMachine.getSnapshot}.
 */
export interface StateMachineSnapshot {
    readonly state: ConductorState;
    readonly history: ReadonlyArray<{ from: ConductorState; event: ConductorEvent; to: ConductorState }>;
}

/**
 * Lightweight finite-state machine for the Conductor extension.
 *
 * Usage:
 * ```ts
 * const fsm = new ConductorStateMachine();
 * fsm.transition(ConductorEvent.BACKEND_CONNECTED); // → ReadyToHost
 * fsm.transition(ConductorEvent.CREATE_WORKSPACE);  // → CreatingWorkspace
 * ```
 */
export class ConductorStateMachine {
    private _state: ConductorState;
    private _history: Array<{ from: ConductorState; event: ConductorEvent; to: ConductorState }> = [];
    private _listeners: StateChangeCallback[] = [];

    constructor(initialState: ConductorState = ConductorState.Idle) {
        this._state = initialState;
    }

    // -----------------------------------------------------------------------
    // State access
    // -----------------------------------------------------------------------

    /** Current state of the machine. */
    get state(): ConductorState {
        return this._state;
    }

    /** Returns the current state (explicit method form for use in tests). */
    getState(): ConductorState {
        return this._state;
    }

    // -----------------------------------------------------------------------
    // Transitions
    // -----------------------------------------------------------------------

    /**
     * Drive a state transition by processing `event`.
     *
     * @param event - The event to process.
     * @returns The new state after the transition.
     * @throws {InvalidTransitionError} When the (currentState, event) pair has no defined transition.
     */
    transition(event: ConductorEvent): ConductorState {
        const key = `${this._state}:${event}`;
        const next = TRANSITION_TABLE[key];
        if (next === undefined) {
            throw new InvalidTransitionError(this._state, event);
        }
        const prev = this._state;
        this._history.push({ from: prev, event, to: next });
        this._state = next;
        // Notify listeners after the state has been updated
        for (const cb of this._listeners.slice()) {
            cb(prev, next, event);
        }
        return next;
    }

    /**
     * @deprecated Use `transition()` instead.
     * Kept for backward compatibility.
     */
    send(event: ConductorEvent): ConductorState {
        return this.transition(event);
    }

    /**
     * Returns true if the given event is valid in the current state
     * without mutating the machine.
     */
    canTransition(event: ConductorEvent): boolean {
        const key = `${this._state}:${event}`;
        return TRANSITION_TABLE[key] !== undefined;
    }

    // -----------------------------------------------------------------------
    // Listeners
    // -----------------------------------------------------------------------

    /**
     * Register a listener that is called after every successful transition.
     *
     * @returns A dispose function that removes the listener.
     */
    onStateChange(cb: StateChangeCallback): () => void {
        this._listeners.push(cb);
        return () => {
            const idx = this._listeners.indexOf(cb);
            if (idx !== -1) this._listeners.splice(idx, 1);
        };
    }

    // -----------------------------------------------------------------------
    // Serialization
    // -----------------------------------------------------------------------

    /**
     * Serialize the current state to a plain string for persistence
     * (e.g., VS Code `globalState`).
     */
    serialize(): string {
        return this._state;
    }

    /**
     * Restore a `ConductorStateMachine` from a serialized state string.
     *
     * @throws {Error} When `serialized` is not a valid `ConductorState` value.
     */
    static deserialize(serialized: string): ConductorStateMachine {
        const validStates = Object.values(ConductorState) as string[];
        if (!validStates.includes(serialized)) {
            throw new Error(
                `Invalid serialized state: "${serialized}". ` +
                `Valid values: ${validStates.join(', ')}`,
            );
        }
        return new ConductorStateMachine(serialized as ConductorState);
    }

    // -----------------------------------------------------------------------
    // Snapshot / reset
    // -----------------------------------------------------------------------

    /**
     * Returns an immutable snapshot of the current state and transition history.
     */
    getSnapshot(): StateMachineSnapshot {
        return {
            state: this._state,
            history: Object.freeze([...this._history]),
        };
    }

    /**
     * Resets the machine back to the {@link ConductorState.Idle} state and
     * clears the transition history.
     */
    reset(): void {
        this._state = ConductorState.Idle;
        this._history = [];
    }
}
