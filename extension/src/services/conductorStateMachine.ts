/**
 * Finite State Machine for the Conductor extension lifecycle.
 *
 * Manages transitions between extension states (Idle, BackendDisconnected,
 * ReadyToHost, Hosting, Joining, Joined) driven by discrete events.
 * Invalid transitions are rejected with an error.
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
}

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

    // From ReadyToHost
    [`${ConductorState.ReadyToHost}:${ConductorEvent.START_HOSTING}`]: ConductorState.Hosting,
    [`${ConductorState.ReadyToHost}:${ConductorEvent.JOIN_SESSION}`]: ConductorState.Joining,
    [`${ConductorState.ReadyToHost}:${ConductorEvent.BACKEND_LOST}`]: ConductorState.BackendDisconnected,

    // From Hosting
    [`${ConductorState.Hosting}:${ConductorEvent.STOP_HOSTING}`]: ConductorState.ReadyToHost,
    [`${ConductorState.Hosting}:${ConductorEvent.BACKEND_LOST}`]: ConductorState.BackendDisconnected,

    // From Joining
    [`${ConductorState.Joining}:${ConductorEvent.JOIN_SUCCEEDED}`]: ConductorState.Joined,
    [`${ConductorState.Joining}:${ConductorEvent.JOIN_FAILED}`]: ConductorState.ReadyToHost,
    [`${ConductorState.Joining}:${ConductorEvent.BACKEND_LOST}`]: ConductorState.BackendDisconnected,

    // From Joined
    [`${ConductorState.Joined}:${ConductorEvent.LEAVE_SESSION}`]: ConductorState.ReadyToHost,
    [`${ConductorState.Joined}:${ConductorEvent.BACKEND_LOST}`]: ConductorState.BackendDisconnected,
};

// ---------------------------------------------------------------------------
// Error type
// ---------------------------------------------------------------------------

/** Error thrown when an invalid state transition is attempted. */
export class InvalidTransitionError extends Error {
    public readonly from: ConductorState;
    public readonly event: ConductorEvent;

    constructor(from: ConductorState, event: ConductorEvent) {
        super(
            `Invalid transition: cannot apply event '${event}' in state '${from}'`
        );
        this.name = 'InvalidTransitionError';
        this.from = from;
        this.event = event;
    }
}

// ---------------------------------------------------------------------------
// Listener type
// ---------------------------------------------------------------------------

/** Callback signature for state-change listeners. */
export type StateChangeListener = (
    prev: ConductorState,
    next: ConductorState,
    event: ConductorEvent,
) => void;

// ---------------------------------------------------------------------------
// State machine class
// ---------------------------------------------------------------------------

/**
 * In-memory finite state machine for the Conductor extension.
 *
 * - Holds the current state (starts at {@link ConductorState.Idle}).
 * - Exposes {@link transition} to apply an event; throws
 *   {@link InvalidTransitionError} for illegal moves.
 * - Supports listeners that are notified **after** every successful transition.
 * - State is serializable via {@link getState} (returns the enum string value).
 */
export class ConductorStateMachine {
    private _state: ConductorState;
    private readonly _listeners: Set<StateChangeListener> = new Set();

    constructor(initialState: ConductorState = ConductorState.Idle) {
        this._state = initialState;
    }

    /** Current state (enum value, JSON-serializable). */
    public getState(): ConductorState {
        return this._state;
    }

    /**
     * Apply an event to the current state.
     *
     * @param event - The event to process.
     * @returns The new state after the transition.
     * @throws {InvalidTransitionError} If the transition is not allowed.
     */
    public transition(event: ConductorEvent): ConductorState {
        const key = `${this._state}:${event}`;
        const next = TRANSITION_TABLE[key];

        if (next === undefined) {
            throw new InvalidTransitionError(this._state, event);
        }

        const prev = this._state;
        this._state = next;

        // Notify listeners after state change
        for (const listener of this._listeners) {
            listener(prev, next, event);
        }

        return this._state;
    }

    /**
     * Register a listener that is called after every successful transition.
     *
     * @param listener - Callback receiving (previousState, newState, event).
     * @returns A dispose function that removes the listener.
     */
    public onStateChange(listener: StateChangeListener): () => void {
        this._listeners.add(listener);
        return () => {
            this._listeners.delete(listener);
        };
    }

    /**
     * Check whether a given event is valid in the current state
     * without actually performing the transition.
     */
    public canTransition(event: ConductorEvent): boolean {
        const key = `${this._state}:${event}`;
        return TRANSITION_TABLE[key] !== undefined;
    }

    /**
     * Serialize the current state to a plain string.
     * Useful for persistence or debugging.
     */
    public serialize(): string {
        return this._state;
    }

    /**
     * Restore state from a previously serialized string.
     *
     * @param serialized - A string that must match a {@link ConductorState} value.
     * @throws {Error} If the string is not a valid state.
     */
    public static deserialize(serialized: string): ConductorStateMachine {
        const values = Object.values(ConductorState) as string[];
        if (!values.includes(serialized)) {
            throw new Error(
                `Invalid serialized state: '${serialized}'. ` +
                `Expected one of: ${values.join(', ')}`
            );
        }
        return new ConductorStateMachine(serialized as ConductorState);
    }
}
