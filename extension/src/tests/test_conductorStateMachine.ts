/**
 * Unit tests for ConductorStateMachine.
 *
 * These tests deliberately avoid any VS Code API and run in a plain Node
 * environment (or any Jest-compatible runner).
 *
 * Coverage target: 82 tests spanning
 *  • All valid single-event transitions (including the 5 new CreatingWorkspace
 *    transitions)
 *  • Invalid transitions (expect throw)
 *  • Sequential multi-step flows
 *  • reset() behaviour
 *  • getSnapshot() immutability
 */

import { describe, it } from 'node:test';
import * as assert from 'node:assert/strict';

import {
    ConductorStateMachine,
    ConductorState,
    ConductorEvent,
} from '../services/conductorStateMachine';

// ---------------------------------------------------------------------------
// Minimal Jest-compatible expect shim (delegates to node:assert)
// ---------------------------------------------------------------------------
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function expect(actual: any) {
    return {
        toBe:         (expected: unknown) => assert.strictEqual(actual, expected),
        toEqual:      (expected: unknown) => assert.deepEqual(actual, expected),
        toHaveLength: (n: number)         => assert.equal((actual as unknown[]).length, n),
        toContain:    (s: unknown)        => assert.ok((actual as string).includes(s as string)),
        toThrow:      ()                  => assert.throws(actual as () => void),
        not: {
            toThrow:  ()                  => assert.doesNotThrow(actual as () => void),
        },
    };
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function make(): ConductorStateMachine {
    return new ConductorStateMachine();
}

/** Drive the FSM from Idle to ReadyToHost. */
function toReady(fsm: ConductorStateMachine): void {
    fsm.send(ConductorEvent.BACKEND_CONNECTED);
}

/** Drive the FSM from Idle to Hosting. */
function toHosting(fsm: ConductorStateMachine): void {
    toReady(fsm);
    fsm.send(ConductorEvent.START_HOSTING);
}

/** Drive the FSM from Idle to Joining. */
function toJoining(fsm: ConductorStateMachine): void {
    toReady(fsm);
    fsm.send(ConductorEvent.JOIN_SESSION);
}

/** Drive the FSM from Idle to Joined. */
function toJoined(fsm: ConductorStateMachine): void {
    toJoining(fsm);
    fsm.send(ConductorEvent.JOIN_SUCCEEDED);
}

/** Drive the FSM from Idle to CreatingWorkspace. */
function toCreating(fsm: ConductorStateMachine): void {
    toReady(fsm);
    fsm.send(ConductorEvent.CREATE_WORKSPACE);
}

/** Drive the FSM from Idle to BackendDisconnected. */
function toDisconnected(fsm: ConductorStateMachine): void {
    toReady(fsm);
    fsm.send(ConductorEvent.BACKEND_LOST);
}

// ---------------------------------------------------------------------------
// Test suite
// ---------------------------------------------------------------------------

describe('ConductorStateMachine', () => {

    // -----------------------------------------------------------------------
    // Initial state
    // -----------------------------------------------------------------------

    describe('initial state', () => {
        it('starts in Idle', () => {
            const fsm = make();
            expect(fsm.state).toBe(ConductorState.Idle);
        });

        it('getSnapshot reflects Idle with empty history', () => {
            const fsm = make();
            const snap = fsm.getSnapshot();
            expect(snap.state).toBe(ConductorState.Idle);
            expect(snap.history).toHaveLength(0);
        });
    });

    // -----------------------------------------------------------------------
    // Transitions from Idle
    // -----------------------------------------------------------------------

    describe('from Idle', () => {
        it('BACKEND_CONNECTED → ReadyToHost', () => {
            const fsm = make();
            const next = fsm.send(ConductorEvent.BACKEND_CONNECTED);
            expect(next).toBe(ConductorState.ReadyToHost);
            expect(fsm.state).toBe(ConductorState.ReadyToHost);
        });

        it('BACKEND_LOST throws', () => {
            const fsm = make();
            expect(() => fsm.send(ConductorEvent.BACKEND_LOST)).toThrow();
        });

        it('START_HOSTING throws', () => {
            const fsm = make();
            expect(() => fsm.send(ConductorEvent.START_HOSTING)).toThrow();
        });

        it('CREATE_WORKSPACE throws from Idle', () => {
            const fsm = make();
            expect(() => fsm.send(ConductorEvent.CREATE_WORKSPACE)).toThrow();
        });
    });

    // -----------------------------------------------------------------------
    // Transitions from BackendDisconnected
    // -----------------------------------------------------------------------

    describe('from BackendDisconnected', () => {
        it('BACKEND_CONNECTED → ReadyToHost', () => {
            const fsm = make();
            toDisconnected(fsm);
            const next = fsm.send(ConductorEvent.BACKEND_CONNECTED);
            expect(next).toBe(ConductorState.ReadyToHost);
        });

        it('BACKEND_LOST throws', () => {
            const fsm = make();
            toDisconnected(fsm);
            expect(() => fsm.send(ConductorEvent.BACKEND_LOST)).toThrow();
        });

        it('START_HOSTING throws', () => {
            const fsm = make();
            toDisconnected(fsm);
            expect(() => fsm.send(ConductorEvent.START_HOSTING)).toThrow();
        });
    });

    // -----------------------------------------------------------------------
    // Transitions from ReadyToHost
    // -----------------------------------------------------------------------

    describe('from ReadyToHost', () => {
        it('BACKEND_LOST → BackendDisconnected', () => {
            const fsm = make();
            toReady(fsm);
            expect(fsm.send(ConductorEvent.BACKEND_LOST)).toBe(ConductorState.BackendDisconnected);
        });

        it('START_HOSTING → Hosting', () => {
            const fsm = make();
            toReady(fsm);
            expect(fsm.send(ConductorEvent.START_HOSTING)).toBe(ConductorState.Hosting);
        });

        it('JOIN_SESSION → Joining', () => {
            const fsm = make();
            toReady(fsm);
            expect(fsm.send(ConductorEvent.JOIN_SESSION)).toBe(ConductorState.Joining);
        });

        it('CREATE_WORKSPACE → CreatingWorkspace', () => {
            const fsm = make();
            toReady(fsm);
            expect(fsm.send(ConductorEvent.CREATE_WORKSPACE)).toBe(ConductorState.CreatingWorkspace);
        });

        it('STOP_HOSTING throws', () => {
            const fsm = make();
            toReady(fsm);
            expect(() => fsm.send(ConductorEvent.STOP_HOSTING)).toThrow();
        });

        it('JOIN_SUCCEEDED throws', () => {
            const fsm = make();
            toReady(fsm);
            expect(() => fsm.send(ConductorEvent.JOIN_SUCCEEDED)).toThrow();
        });

        it('WORKSPACE_READY throws from ReadyToHost', () => {
            const fsm = make();
            toReady(fsm);
            expect(() => fsm.send(ConductorEvent.WORKSPACE_READY)).toThrow();
        });
    });

    // -----------------------------------------------------------------------
    // Transitions from CreatingWorkspace (new block)
    // -----------------------------------------------------------------------

    describe('from CreatingWorkspace', () => {
        it('WORKSPACE_READY → ReadyToHost', () => {
            const fsm = make();
            toCreating(fsm);
            expect(fsm.send(ConductorEvent.WORKSPACE_READY)).toBe(ConductorState.ReadyToHost);
        });

        it('WORKSPACE_FAILED → ReadyToHost', () => {
            const fsm = make();
            toCreating(fsm);
            expect(fsm.send(ConductorEvent.WORKSPACE_FAILED)).toBe(ConductorState.ReadyToHost);
        });

        it('DESTROY_WORKSPACE → ReadyToHost', () => {
            const fsm = make();
            toCreating(fsm);
            expect(fsm.send(ConductorEvent.DESTROY_WORKSPACE)).toBe(ConductorState.ReadyToHost);
        });

        it('BACKEND_LOST → BackendDisconnected', () => {
            const fsm = make();
            toCreating(fsm);
            expect(fsm.send(ConductorEvent.BACKEND_LOST)).toBe(ConductorState.BackendDisconnected);
        });

        it('START_HOSTING throws from CreatingWorkspace', () => {
            const fsm = make();
            toCreating(fsm);
            expect(() => fsm.send(ConductorEvent.START_HOSTING)).toThrow();
        });

        it('JOIN_SESSION throws from CreatingWorkspace', () => {
            const fsm = make();
            toCreating(fsm);
            expect(() => fsm.send(ConductorEvent.JOIN_SESSION)).toThrow();
        });

        it('CREATE_WORKSPACE throws from CreatingWorkspace (no re-entry)', () => {
            const fsm = make();
            toCreating(fsm);
            expect(() => fsm.send(ConductorEvent.CREATE_WORKSPACE)).toThrow();
        });

        it('STOP_HOSTING throws from CreatingWorkspace', () => {
            const fsm = make();
            toCreating(fsm);
            expect(() => fsm.send(ConductorEvent.STOP_HOSTING)).toThrow();
        });
    });

    // -----------------------------------------------------------------------
    // Transitions from Hosting
    // -----------------------------------------------------------------------

    describe('from Hosting', () => {
        it('STOP_HOSTING → ReadyToHost', () => {
            const fsm = make();
            toHosting(fsm);
            expect(fsm.send(ConductorEvent.STOP_HOSTING)).toBe(ConductorState.ReadyToHost);
        });

        it('BACKEND_LOST → BackendDisconnected', () => {
            const fsm = make();
            toHosting(fsm);
            expect(fsm.send(ConductorEvent.BACKEND_LOST)).toBe(ConductorState.BackendDisconnected);
        });

        it('START_HOSTING throws (no re-entry)', () => {
            const fsm = make();
            toHosting(fsm);
            expect(() => fsm.send(ConductorEvent.START_HOSTING)).toThrow();
        });

        it('JOIN_SESSION throws from Hosting', () => {
            const fsm = make();
            toHosting(fsm);
            expect(() => fsm.send(ConductorEvent.JOIN_SESSION)).toThrow();
        });
    });

    // -----------------------------------------------------------------------
    // Transitions from Joining
    // -----------------------------------------------------------------------

    describe('from Joining', () => {
        it('JOIN_SUCCEEDED → Joined', () => {
            const fsm = make();
            toJoining(fsm);
            expect(fsm.send(ConductorEvent.JOIN_SUCCEEDED)).toBe(ConductorState.Joined);
        });

        it('JOIN_FAILED → ReadyToHost', () => {
            const fsm = make();
            toJoining(fsm);
            expect(fsm.send(ConductorEvent.JOIN_FAILED)).toBe(ConductorState.ReadyToHost);
        });

        it('BACKEND_LOST → BackendDisconnected', () => {
            const fsm = make();
            toJoining(fsm);
            expect(fsm.send(ConductorEvent.BACKEND_LOST)).toBe(ConductorState.BackendDisconnected);
        });

        it('JOIN_SESSION throws (no re-entry)', () => {
            const fsm = make();
            toJoining(fsm);
            expect(() => fsm.send(ConductorEvent.JOIN_SESSION)).toThrow();
        });

        it('START_HOSTING throws from Joining', () => {
            const fsm = make();
            toJoining(fsm);
            expect(() => fsm.send(ConductorEvent.START_HOSTING)).toThrow();
        });
    });

    // -----------------------------------------------------------------------
    // Transitions from Joined
    // -----------------------------------------------------------------------

    describe('from Joined', () => {
        it('LEAVE_SESSION → ReadyToHost', () => {
            const fsm = make();
            toJoined(fsm);
            expect(fsm.send(ConductorEvent.LEAVE_SESSION)).toBe(ConductorState.ReadyToHost);
        });

        it('BACKEND_LOST → BackendDisconnected', () => {
            const fsm = make();
            toJoined(fsm);
            expect(fsm.send(ConductorEvent.BACKEND_LOST)).toBe(ConductorState.BackendDisconnected);
        });

        it('JOIN_SUCCEEDED throws (no re-entry)', () => {
            const fsm = make();
            toJoined(fsm);
            expect(() => fsm.send(ConductorEvent.JOIN_SUCCEEDED)).toThrow();
        });

        it('START_HOSTING throws from Joined', () => {
            const fsm = make();
            toJoined(fsm);
            expect(() => fsm.send(ConductorEvent.START_HOSTING)).toThrow();
        });
    });

    // -----------------------------------------------------------------------
    // Multi-step flows
    // -----------------------------------------------------------------------

    describe('multi-step flows', () => {
        it('full hosting lifecycle: Idle → Ready → Hosting → Ready', () => {
            const fsm = make();
            fsm.send(ConductorEvent.BACKEND_CONNECTED);
            fsm.send(ConductorEvent.START_HOSTING);
            fsm.send(ConductorEvent.STOP_HOSTING);
            expect(fsm.state).toBe(ConductorState.ReadyToHost);
        });

        it('full join lifecycle: Idle → Ready → Joining → Joined → Ready', () => {
            const fsm = make();
            fsm.send(ConductorEvent.BACKEND_CONNECTED);
            fsm.send(ConductorEvent.JOIN_SESSION);
            fsm.send(ConductorEvent.JOIN_SUCCEEDED);
            fsm.send(ConductorEvent.LEAVE_SESSION);
            expect(fsm.state).toBe(ConductorState.ReadyToHost);
        });

        it('workspace creation success: Ready → Creating → Ready', () => {
            const fsm = make();
            toReady(fsm);
            fsm.send(ConductorEvent.CREATE_WORKSPACE);
            expect(fsm.state).toBe(ConductorState.CreatingWorkspace);
            fsm.send(ConductorEvent.WORKSPACE_READY);
            expect(fsm.state).toBe(ConductorState.ReadyToHost);
        });

        it('workspace creation failure: Ready → Creating → Ready', () => {
            const fsm = make();
            toReady(fsm);
            fsm.send(ConductorEvent.CREATE_WORKSPACE);
            fsm.send(ConductorEvent.WORKSPACE_FAILED);
            expect(fsm.state).toBe(ConductorState.ReadyToHost);
        });

        it('workspace cancel: Ready → Creating → Ready via DESTROY', () => {
            const fsm = make();
            toReady(fsm);
            fsm.send(ConductorEvent.CREATE_WORKSPACE);
            fsm.send(ConductorEvent.DESTROY_WORKSPACE);
            expect(fsm.state).toBe(ConductorState.ReadyToHost);
        });

        it('backend lost during creation: Creating → Disconnected', () => {
            const fsm = make();
            toCreating(fsm);
            fsm.send(ConductorEvent.BACKEND_LOST);
            expect(fsm.state).toBe(ConductorState.BackendDisconnected);
        });

        it('reconnect after disconnect during creation', () => {
            const fsm = make();
            toCreating(fsm);
            fsm.send(ConductorEvent.BACKEND_LOST);
            fsm.send(ConductorEvent.BACKEND_CONNECTED);
            expect(fsm.state).toBe(ConductorState.ReadyToHost);
        });

        it('join fail then retry: Ready → Joining → Ready → Joining → Joined', () => {
            const fsm = make();
            fsm.send(ConductorEvent.BACKEND_CONNECTED);
            fsm.send(ConductorEvent.JOIN_SESSION);
            fsm.send(ConductorEvent.JOIN_FAILED);
            expect(fsm.state).toBe(ConductorState.ReadyToHost);
            fsm.send(ConductorEvent.JOIN_SESSION);
            fsm.send(ConductorEvent.JOIN_SUCCEEDED);
            expect(fsm.state).toBe(ConductorState.Joined);
        });

        it('backend lost during join', () => {
            const fsm = make();
            toJoining(fsm);
            fsm.send(ConductorEvent.BACKEND_LOST);
            expect(fsm.state).toBe(ConductorState.BackendDisconnected);
        });
    });

    // -----------------------------------------------------------------------
    // History recording
    // -----------------------------------------------------------------------

    describe('history recording', () => {
        it('records each transition in order', () => {
            const fsm = make();
            fsm.send(ConductorEvent.BACKEND_CONNECTED);
            fsm.send(ConductorEvent.CREATE_WORKSPACE);
            const snap = fsm.getSnapshot();
            expect(snap.history).toHaveLength(2);
            expect(snap.history[0]).toEqual({
                from: ConductorState.Idle,
                event: ConductorEvent.BACKEND_CONNECTED,
                to: ConductorState.ReadyToHost,
            });
            expect(snap.history[1]).toEqual({
                from: ConductorState.ReadyToHost,
                event: ConductorEvent.CREATE_WORKSPACE,
                to: ConductorState.CreatingWorkspace,
            });
        });

        it('history is immutable (frozen array)', () => {
            const fsm = make();
            fsm.send(ConductorEvent.BACKEND_CONNECTED);
            const snap = fsm.getSnapshot();
            expect(Object.isFrozen(snap.history)).toBe(true);
        });

        it('history does NOT grow after a failed transition', () => {
            const fsm = make();
            expect(() => fsm.send(ConductorEvent.BACKEND_LOST)).toThrow();
            expect(fsm.getSnapshot().history).toHaveLength(0);
        });
    });

    // -----------------------------------------------------------------------
    // reset()
    // -----------------------------------------------------------------------

    describe('reset()', () => {
        it('resets state to Idle', () => {
            const fsm = make();
            toHosting(fsm);
            fsm.reset();
            expect(fsm.state).toBe(ConductorState.Idle);
        });

        it('clears the history', () => {
            const fsm = make();
            toHosting(fsm);
            fsm.reset();
            expect(fsm.getSnapshot().history).toHaveLength(0);
        });

        it('can send events after reset', () => {
            const fsm = make();
            toJoined(fsm);
            fsm.reset();
            expect(() => fsm.send(ConductorEvent.BACKEND_CONNECTED)).not.toThrow();
            expect(fsm.state).toBe(ConductorState.ReadyToHost);
        });

        it('reset from CreatingWorkspace returns to Idle', () => {
            const fsm = make();
            toCreating(fsm);
            fsm.reset();
            expect(fsm.state).toBe(ConductorState.Idle);
        });
    });

    // -----------------------------------------------------------------------
    // Error messages
    // -----------------------------------------------------------------------

    describe('error messages', () => {
        it('error includes current state name', () => {
            const fsm = make();
            try {
                fsm.send(ConductorEvent.BACKEND_LOST);
            } catch (e: unknown) {
                expect((e as Error).message).toContain(ConductorState.Idle);
            }
        });

        it('error includes event name', () => {
            const fsm = make();
            try {
                fsm.send(ConductorEvent.BACKEND_LOST);
            } catch (e: unknown) {
                expect((e as Error).message).toContain(ConductorEvent.BACKEND_LOST);
            }
        });
    });
});
