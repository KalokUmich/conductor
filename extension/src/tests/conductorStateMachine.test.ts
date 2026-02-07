/**
 * Unit tests for ConductorStateMachine.
 *
 * Run after compilation:
 *   node --test out/tests/conductorStateMachine.test.js
 */
import { describe, it, beforeEach } from 'node:test';
import * as assert from 'node:assert/strict';

import {
    ConductorState,
    ConductorEvent,
    ConductorStateMachine,
    InvalidTransitionError,
} from '../services/conductorStateMachine';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Shorthand aliases for readability. */
const S = ConductorState;
const E = ConductorEvent;

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('ConductorStateMachine', () => {
    let fsm: ConductorStateMachine;

    beforeEach(() => {
        fsm = new ConductorStateMachine();
    });

    // -----------------------------------------------------------------------
    // Initial state
    // -----------------------------------------------------------------------

    describe('initial state', () => {
        it('starts in Idle by default', () => {
            assert.equal(fsm.getState(), S.Idle);
        });

        it('accepts a custom initial state', () => {
            const custom = new ConductorStateMachine(S.Hosting);
            assert.equal(custom.getState(), S.Hosting);
        });
    });

    // -----------------------------------------------------------------------
    // Valid transitions – happy paths
    // -----------------------------------------------------------------------

    describe('valid transitions', () => {
        it('Idle → ReadyToHost on BACKEND_CONNECTED', () => {
            assert.equal(fsm.transition(E.BACKEND_CONNECTED), S.ReadyToHost);
        });

        it('Idle → BackendDisconnected on BACKEND_LOST', () => {
            assert.equal(fsm.transition(E.BACKEND_LOST), S.BackendDisconnected);
        });

        it('BackendDisconnected → ReadyToHost on BACKEND_CONNECTED', () => {
            fsm.transition(E.BACKEND_LOST);
            assert.equal(fsm.transition(E.BACKEND_CONNECTED), S.ReadyToHost);
        });

        it('ReadyToHost → Hosting on START_HOSTING', () => {
            fsm.transition(E.BACKEND_CONNECTED);
            assert.equal(fsm.transition(E.START_HOSTING), S.Hosting);
        });

        it('ReadyToHost → Joining on JOIN_SESSION', () => {
            fsm.transition(E.BACKEND_CONNECTED);
            assert.equal(fsm.transition(E.JOIN_SESSION), S.Joining);
        });

        it('ReadyToHost → BackendDisconnected on BACKEND_LOST', () => {
            fsm.transition(E.BACKEND_CONNECTED);
            assert.equal(fsm.transition(E.BACKEND_LOST), S.BackendDisconnected);
        });

        it('Hosting → ReadyToHost on STOP_HOSTING', () => {
            fsm.transition(E.BACKEND_CONNECTED);
            fsm.transition(E.START_HOSTING);
            assert.equal(fsm.transition(E.STOP_HOSTING), S.ReadyToHost);
        });

        it('Hosting → BackendDisconnected on BACKEND_LOST', () => {
            fsm.transition(E.BACKEND_CONNECTED);
            fsm.transition(E.START_HOSTING);
            assert.equal(fsm.transition(E.BACKEND_LOST), S.BackendDisconnected);
        });

        it('Joining → Joined on JOIN_SUCCEEDED', () => {
            fsm.transition(E.BACKEND_CONNECTED);
            fsm.transition(E.JOIN_SESSION);
            assert.equal(fsm.transition(E.JOIN_SUCCEEDED), S.Joined);
        });

        it('Joining → ReadyToHost on JOIN_FAILED', () => {
            fsm.transition(E.BACKEND_CONNECTED);
            fsm.transition(E.JOIN_SESSION);
            assert.equal(fsm.transition(E.JOIN_FAILED), S.ReadyToHost);
        });

        it('Joining → BackendDisconnected on BACKEND_LOST', () => {
            fsm.transition(E.BACKEND_CONNECTED);
            fsm.transition(E.JOIN_SESSION);
            assert.equal(fsm.transition(E.BACKEND_LOST), S.BackendDisconnected);
        });

        it('Joined → ReadyToHost on LEAVE_SESSION', () => {
            fsm.transition(E.BACKEND_CONNECTED);
            fsm.transition(E.JOIN_SESSION);
            fsm.transition(E.JOIN_SUCCEEDED);
            assert.equal(fsm.transition(E.LEAVE_SESSION), S.ReadyToHost);
        });

        it('Joined → BackendDisconnected on BACKEND_LOST', () => {
            fsm.transition(E.BACKEND_CONNECTED);
            fsm.transition(E.JOIN_SESSION);
            fsm.transition(E.JOIN_SUCCEEDED);
            assert.equal(fsm.transition(E.BACKEND_LOST), S.BackendDisconnected);
        });
    });

    // -----------------------------------------------------------------------
    // Full lifecycle paths
    // -----------------------------------------------------------------------

    describe('full lifecycle paths', () => {
        it('host lifecycle: Idle → ReadyToHost → Hosting → ReadyToHost', () => {
            fsm.transition(E.BACKEND_CONNECTED);
            fsm.transition(E.START_HOSTING);
            fsm.transition(E.STOP_HOSTING);
            assert.equal(fsm.getState(), S.ReadyToHost);
        });

        it('guest lifecycle: Idle → ReadyToHost → Joining → Joined → ReadyToHost', () => {
            fsm.transition(E.BACKEND_CONNECTED);
            fsm.transition(E.JOIN_SESSION);
            fsm.transition(E.JOIN_SUCCEEDED);
            fsm.transition(E.LEAVE_SESSION);
            assert.equal(fsm.getState(), S.ReadyToHost);
        });

        it('reconnect after disconnect while hosting', () => {
            fsm.transition(E.BACKEND_CONNECTED);
            fsm.transition(E.START_HOSTING);
            fsm.transition(E.BACKEND_LOST);
            assert.equal(fsm.getState(), S.BackendDisconnected);
            fsm.transition(E.BACKEND_CONNECTED);
            assert.equal(fsm.getState(), S.ReadyToHost);
        });
    });

    // -----------------------------------------------------------------------
    // Invalid transitions
    // -----------------------------------------------------------------------

    describe('invalid transitions', () => {
        it('throws InvalidTransitionError for START_HOSTING in Idle', () => {
            assert.throws(
                () => fsm.transition(E.START_HOSTING),
                (err: unknown) => {
                    assert.ok(err instanceof InvalidTransitionError);
                    assert.equal(err.from, S.Idle);
                    assert.equal(err.event, E.START_HOSTING);
                    return true;
                },
            );
        });

        it('throws for JOIN_SESSION in Idle (backend not connected)', () => {
            assert.throws(
                () => fsm.transition(E.JOIN_SESSION),
                InvalidTransitionError,
            );
        });

        it('throws for STOP_HOSTING in ReadyToHost', () => {
            fsm.transition(E.BACKEND_CONNECTED);
            assert.throws(
                () => fsm.transition(E.STOP_HOSTING),
                InvalidTransitionError,
            );
        });

        it('throws for JOIN_SUCCEEDED in Hosting', () => {
            fsm.transition(E.BACKEND_CONNECTED);
            fsm.transition(E.START_HOSTING);
            assert.throws(
                () => fsm.transition(E.JOIN_SUCCEEDED),
                InvalidTransitionError,
            );
        });

        it('throws for LEAVE_SESSION in Joining', () => {
            fsm.transition(E.BACKEND_CONNECTED);
            fsm.transition(E.JOIN_SESSION);
            assert.throws(
                () => fsm.transition(E.LEAVE_SESSION),
                InvalidTransitionError,
            );
        });

        it('does not change state on invalid transition', () => {
            fsm.transition(E.BACKEND_CONNECTED);
            const before = fsm.getState();
            try { fsm.transition(E.LEAVE_SESSION); } catch { /* expected */ }
            assert.equal(fsm.getState(), before);
        });
    });

    // -----------------------------------------------------------------------
    // canTransition
    // -----------------------------------------------------------------------

    describe('canTransition', () => {
        it('returns true for valid event in current state', () => {
            assert.equal(fsm.canTransition(E.BACKEND_CONNECTED), true);
        });

        it('returns false for invalid event in current state', () => {
            assert.equal(fsm.canTransition(E.START_HOSTING), false);
        });

        it('does not mutate state', () => {
            fsm.canTransition(E.BACKEND_CONNECTED);
            assert.equal(fsm.getState(), S.Idle);
        });
    });

    // -----------------------------------------------------------------------
    // Listeners
    // -----------------------------------------------------------------------

    describe('onStateChange listeners', () => {
        it('notifies listener on valid transition', () => {
            const calls: Array<[ConductorState, ConductorState, ConductorEvent]> = [];
            fsm.onStateChange((prev, next, event) => calls.push([prev, next, event]));

            fsm.transition(E.BACKEND_CONNECTED);

            assert.equal(calls.length, 1);
            assert.deepEqual(calls[0], [S.Idle, S.ReadyToHost, E.BACKEND_CONNECTED]);
        });

        it('does not notify listener on invalid transition', () => {
            let called = false;
            fsm.onStateChange(() => { called = true; });

            try { fsm.transition(E.START_HOSTING); } catch { /* expected */ }

            assert.equal(called, false);
        });

        it('supports multiple listeners', () => {
            let count = 0;
            fsm.onStateChange(() => { count++; });
            fsm.onStateChange(() => { count++; });

            fsm.transition(E.BACKEND_CONNECTED);
            assert.equal(count, 2);
        });

        it('dispose function removes the listener', () => {
            let count = 0;
            const dispose = fsm.onStateChange(() => { count++; });

            fsm.transition(E.BACKEND_CONNECTED);
            assert.equal(count, 1);

            dispose();
            fsm.transition(E.START_HOSTING);
            assert.equal(count, 1); // not incremented
        });
    });

    // -----------------------------------------------------------------------
    // Serialization
    // -----------------------------------------------------------------------

    describe('serialization', () => {
        it('serialize returns the current state string', () => {
            assert.equal(fsm.serialize(), 'Idle');
            fsm.transition(E.BACKEND_CONNECTED);
            assert.equal(fsm.serialize(), 'ReadyToHost');
        });

        it('deserialize restores a machine in the given state', () => {
            const restored = ConductorStateMachine.deserialize('Hosting');
            assert.equal(restored.getState(), S.Hosting);
        });

        it('deserialize throws for invalid state string', () => {
            assert.throws(
                () => ConductorStateMachine.deserialize('InvalidState'),
                /Invalid serialized state/,
            );
        });

        it('round-trip: serialize then deserialize preserves state', () => {
            fsm.transition(E.BACKEND_CONNECTED);
            fsm.transition(E.JOIN_SESSION);
            fsm.transition(E.JOIN_SUCCEEDED);

            const serialized = fsm.serialize();
            const restored = ConductorStateMachine.deserialize(serialized);
            assert.equal(restored.getState(), fsm.getState());
        });
    });
});
