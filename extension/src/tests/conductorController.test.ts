/**
 * Unit tests for ConductorController.
 *
 * Uses real ConductorStateMachine instances but injects mock health check
 * functions so no real HTTP calls are made.
 *
 * Run after compilation:
 *   node --test out/tests/conductorController.test.js
 */
import { describe, it, beforeEach } from 'node:test';
import * as assert from 'node:assert/strict';

import {
    ConductorState,
    ConductorEvent,
    ConductorStateMachine,
} from '../services/conductorStateMachine';
import { ConductorController } from '../services/conductorController';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const S = ConductorState;
const BACKEND_URL = 'http://localhost:8000';

/** Mock health check that always succeeds. */
const healthyCheck = async (_url: string): Promise<boolean> => true;

/** Mock health check that always fails. */
const unhealthyCheck = async (_url: string): Promise<boolean> => false;

/** URL provider that returns a fixed URL. */
const urlProvider = (): string => BACKEND_URL;

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('ConductorController', () => {
    let fsm: ConductorStateMachine;

    beforeEach(() => {
        fsm = new ConductorStateMachine();
    });

    // -----------------------------------------------------------------------
    // start() – healthy backend
    // -----------------------------------------------------------------------

    describe('start() with healthy backend', () => {
        it('transitions from Idle to ReadyToHost', async () => {
            const ctrl = new ConductorController(fsm, healthyCheck, urlProvider);
            const newState = await ctrl.start();

            assert.equal(newState, S.ReadyToHost);
            assert.equal(ctrl.getState(), S.ReadyToHost);
        });

        it('transitions from BackendDisconnected to ReadyToHost', async () => {
            // Put FSM into BackendDisconnected first
            fsm.transition(ConductorEvent.BACKEND_LOST);
            assert.equal(fsm.getState(), S.BackendDisconnected);

            const ctrl = new ConductorController(fsm, healthyCheck, urlProvider);
            const newState = await ctrl.start();

            assert.equal(newState, S.ReadyToHost);
        });
    });

    // -----------------------------------------------------------------------
    // start() – unhealthy backend
    // -----------------------------------------------------------------------

    describe('start() with unhealthy backend', () => {
        it('transitions from Idle to BackendDisconnected', async () => {
            const ctrl = new ConductorController(fsm, unhealthyCheck, urlProvider);
            const newState = await ctrl.start();

            assert.equal(newState, S.BackendDisconnected);
            assert.equal(ctrl.getState(), S.BackendDisconnected);
        });
    });

    // -----------------------------------------------------------------------
    // start() – invalid starting state
    // -----------------------------------------------------------------------

    describe('start() from invalid state', () => {
        it('throws when FSM is in ReadyToHost', async () => {
            fsm.transition(ConductorEvent.BACKEND_CONNECTED);
            const ctrl = new ConductorController(fsm, healthyCheck, urlProvider);

            await assert.rejects(
                () => ctrl.start(),
                /Cannot start from state/,
            );
        });

        it('throws when FSM is in Hosting', async () => {
            fsm.transition(ConductorEvent.BACKEND_CONNECTED);
            fsm.transition(ConductorEvent.START_HOSTING);
            const ctrl = new ConductorController(fsm, healthyCheck, urlProvider);

            await assert.rejects(
                () => ctrl.start(),
                /Cannot start from state/,
            );
        });

        it('does not change state on invalid start', async () => {
            fsm.transition(ConductorEvent.BACKEND_CONNECTED);
            const before = fsm.getState();
            const ctrl = new ConductorController(fsm, healthyCheck, urlProvider);

            try { await ctrl.start(); } catch { /* expected */ }
            assert.equal(fsm.getState(), before);
        });
    });

    // -----------------------------------------------------------------------
    // URL provider is called with correct URL
    // -----------------------------------------------------------------------

    describe('URL provider integration', () => {
        it('passes URL from provider to health check', async () => {
            let receivedUrl = '';
            const capturingCheck = async (url: string): Promise<boolean> => {
                receivedUrl = url;
                return true;
            };
            const customUrl = 'http://my-backend:9999';
            const ctrl = new ConductorController(
                fsm,
                capturingCheck,
                () => customUrl,
            );

            await ctrl.start();
            assert.equal(receivedUrl, customUrl);
        });
    });

    // -----------------------------------------------------------------------
    // Listener notifications
    // -----------------------------------------------------------------------

    describe('onStateChange', () => {
        it('notifies listener when start succeeds', async () => {
            const ctrl = new ConductorController(fsm, healthyCheck, urlProvider);
            const transitions: ConductorState[] = [];
            ctrl.onStateChange((_prev, next) => transitions.push(next));

            await ctrl.start();

            assert.equal(transitions.length, 1);
            assert.equal(transitions[0], S.ReadyToHost);
        });

        it('notifies listener when start fails', async () => {
            const ctrl = new ConductorController(fsm, unhealthyCheck, urlProvider);
            const transitions: ConductorState[] = [];
            ctrl.onStateChange((_prev, next) => transitions.push(next));

            await ctrl.start();

            assert.equal(transitions.length, 1);
            assert.equal(transitions[0], S.BackendDisconnected);
        });
    });

    // -----------------------------------------------------------------------
    // startHosting()
    // -----------------------------------------------------------------------

    describe('startHosting()', () => {
        /** Helper: bring FSM to ReadyToHost via a healthy start(). */
        async function readyController(
            sessionResetFn?: () => string,
        ): Promise<ConductorController> {
            const ctrl = new ConductorController(
                fsm,
                healthyCheck,
                urlProvider,
                sessionResetFn,
            );
            await ctrl.start();
            assert.equal(ctrl.getState(), S.ReadyToHost);
            return ctrl;
        }

        it('transitions from ReadyToHost to Hosting', async () => {
            const ctrl = await readyController(() => 'room-abc');
            const roomId = ctrl.startHosting();

            assert.equal(roomId, 'room-abc');
            assert.equal(ctrl.getState(), S.Hosting);
        });

        it('calls sessionReset and returns its value', async () => {
            let called = false;
            const ctrl = await readyController(() => {
                called = true;
                return 'fresh-room-id';
            });

            const roomId = ctrl.startHosting();
            assert.ok(called, 'sessionReset must be called');
            assert.equal(roomId, 'fresh-room-id');
        });

        it('throws when FSM is in Idle', () => {
            const ctrl = new ConductorController(fsm, healthyCheck, urlProvider);
            assert.throws(
                () => ctrl.startHosting(),
                /Cannot start hosting from state/,
            );
        });

        it('throws when FSM is in Hosting', async () => {
            const ctrl = await readyController(() => 'r1');
            ctrl.startHosting(); // now Hosting
            assert.throws(
                () => ctrl.startHosting(),
                /Cannot start hosting from state/,
            );
        });

        it('does not mutate state on invalid call', () => {
            const ctrl = new ConductorController(fsm, healthyCheck, urlProvider);
            const before = ctrl.getState(); // Idle
            try { ctrl.startHosting(); } catch { /* expected */ }
            assert.equal(ctrl.getState(), before);
        });
    });

    // -----------------------------------------------------------------------
    // stopHosting()
    // -----------------------------------------------------------------------

    describe('stopHosting()', () => {
        async function hostingController(): Promise<ConductorController> {
            const ctrl = new ConductorController(
                fsm,
                healthyCheck,
                urlProvider,
                () => 'room-xyz',
            );
            await ctrl.start();
            ctrl.startHosting();
            assert.equal(ctrl.getState(), S.Hosting);
            return ctrl;
        }

        it('transitions from Hosting to ReadyToHost', async () => {
            const ctrl = await hostingController();
            ctrl.stopHosting();
            assert.equal(ctrl.getState(), S.ReadyToHost);
        });

        it('throws when FSM is in ReadyToHost', async () => {
            const ctrl = new ConductorController(fsm, healthyCheck, urlProvider);
            await ctrl.start();
            assert.equal(ctrl.getState(), S.ReadyToHost);
            assert.throws(
                () => ctrl.stopHosting(),
                /Cannot stop hosting from state/,
            );
        });

        it('throws when FSM is in Idle', () => {
            const ctrl = new ConductorController(fsm, healthyCheck, urlProvider);
            assert.throws(
                () => ctrl.stopHosting(),
                /Cannot stop hosting from state/,
            );
        });

        it('does not mutate state on invalid call', async () => {
            const ctrl = new ConductorController(fsm, healthyCheck, urlProvider);
            await ctrl.start(); // ReadyToHost
            const before = ctrl.getState();
            try { ctrl.stopHosting(); } catch { /* expected */ }
            assert.equal(ctrl.getState(), before);
        });
    });

    // -----------------------------------------------------------------------
    // canTransition()
    // -----------------------------------------------------------------------

    describe('canTransition()', () => {
        it('returns true for valid transition', async () => {
            const ctrl = new ConductorController(fsm, healthyCheck, urlProvider);
            await ctrl.start(); // ReadyToHost
            assert.ok(ctrl.canTransition(ConductorEvent.START_HOSTING));
        });

        it('returns false for invalid transition', async () => {
            const ctrl = new ConductorController(fsm, healthyCheck, urlProvider);
            await ctrl.start(); // ReadyToHost
            assert.ok(!ctrl.canTransition(ConductorEvent.BACKEND_CONNECTED));
        });
    });

    // -----------------------------------------------------------------------
    // startJoining()
    // -----------------------------------------------------------------------

    describe('startJoining()', () => {
        const INVITE_URL =
            'https://example.ngrok.dev/invite?roomId=abc-123&liveShareUrl=https%3A%2F%2Fprod.liveshare.vsengsaas.visualstudio.com%2Fjoin%3FABC123';

        async function readyController(): Promise<ConductorController> {
            const ctrl = new ConductorController(fsm, healthyCheck, urlProvider);
            await ctrl.start();
            assert.equal(ctrl.getState(), S.ReadyToHost);
            return ctrl;
        }

        it('transitions from ReadyToHost to Joining', async () => {
            const ctrl = await readyController();
            ctrl.startJoining(INVITE_URL);
            assert.equal(ctrl.getState(), S.Joining);
        });

        it('returns parsed invite with roomId, backendUrl, liveShareUrl', async () => {
            const ctrl = await readyController();
            const parsed = ctrl.startJoining(INVITE_URL);
            assert.equal(parsed.roomId, 'abc-123');
            assert.equal(parsed.backendUrl, 'https://example.ngrok.dev');
            assert.equal(
                parsed.liveShareUrl,
                'https://prod.liveshare.vsengsaas.visualstudio.com/join?ABC123',
            );
        });

        it('works without liveShareUrl param', async () => {
            const ctrl = await readyController();
            const parsed = ctrl.startJoining(
                'http://localhost:8000/invite?roomId=room-xyz',
            );
            assert.equal(parsed.roomId, 'room-xyz');
            assert.equal(parsed.backendUrl, 'http://localhost:8000');
            assert.equal(parsed.liveShareUrl, undefined);
        });

        it('throws for invalid URL', async () => {
            const ctrl = await readyController();
            assert.throws(
                () => ctrl.startJoining('not-a-url'),
                /Invalid invite URL/,
            );
            // State must remain ReadyToHost
            assert.equal(ctrl.getState(), S.ReadyToHost);
        });

        it('throws when roomId is missing', async () => {
            const ctrl = await readyController();
            assert.throws(
                () => ctrl.startJoining('https://example.com/invite'),
                /missing the 'roomId'/,
            );
            assert.equal(ctrl.getState(), S.ReadyToHost);
        });

        it('throws when FSM is in Idle', () => {
            const ctrl = new ConductorController(fsm, healthyCheck, urlProvider);
            assert.throws(
                () => ctrl.startJoining(INVITE_URL),
                /Cannot join session from state/,
            );
        });

        it('throws when FSM is in Hosting', async () => {
            const ctrl = new ConductorController(
                fsm, healthyCheck, urlProvider, () => 'r1',
            );
            await ctrl.start();
            ctrl.startHosting();
            assert.throws(
                () => ctrl.startJoining(INVITE_URL),
                /Cannot join session from state/,
            );
        });
    });

    // -----------------------------------------------------------------------
    // joinSucceeded()
    // -----------------------------------------------------------------------

    describe('joinSucceeded()', () => {
        async function joiningController(): Promise<ConductorController> {
            const ctrl = new ConductorController(fsm, healthyCheck, urlProvider);
            await ctrl.start();
            ctrl.startJoining(
                'http://localhost:8000/invite?roomId=r1',
            );
            assert.equal(ctrl.getState(), S.Joining);
            return ctrl;
        }

        it('transitions from Joining to Joined', async () => {
            const ctrl = await joiningController();
            ctrl.joinSucceeded();
            assert.equal(ctrl.getState(), S.Joined);
        });

        it('throws when FSM is in ReadyToHost', async () => {
            const ctrl = new ConductorController(fsm, healthyCheck, urlProvider);
            await ctrl.start();
            assert.throws(
                () => ctrl.joinSucceeded(),
                /Cannot mark join as succeeded from state/,
            );
        });
    });

    // -----------------------------------------------------------------------
    // joinFailed()
    // -----------------------------------------------------------------------

    describe('joinFailed()', () => {
        async function joiningController(): Promise<ConductorController> {
            const ctrl = new ConductorController(fsm, healthyCheck, urlProvider);
            await ctrl.start();
            ctrl.startJoining(
                'http://localhost:8000/invite?roomId=r1',
            );
            assert.equal(ctrl.getState(), S.Joining);
            return ctrl;
        }

        it('transitions from Joining to ReadyToHost', async () => {
            const ctrl = await joiningController();
            ctrl.joinFailed();
            assert.equal(ctrl.getState(), S.ReadyToHost);
        });

        it('throws when FSM is not in Joining', async () => {
            const ctrl = new ConductorController(fsm, healthyCheck, urlProvider);
            await ctrl.start();
            assert.throws(
                () => ctrl.joinFailed(),
                /Cannot mark join as failed from state/,
            );
        });
    });

    // -----------------------------------------------------------------------
    // leaveSession()
    // -----------------------------------------------------------------------

    describe('leaveSession()', () => {
        async function joinedController(): Promise<ConductorController> {
            const ctrl = new ConductorController(fsm, healthyCheck, urlProvider);
            await ctrl.start();
            ctrl.startJoining(
                'http://localhost:8000/invite?roomId=r1',
            );
            ctrl.joinSucceeded();
            assert.equal(ctrl.getState(), S.Joined);
            return ctrl;
        }

        it('transitions from Joined to ReadyToHost', async () => {
            const ctrl = await joinedController();
            ctrl.leaveSession();
            assert.equal(ctrl.getState(), S.ReadyToHost);
        });

        it('throws when FSM is in Hosting', async () => {
            const ctrl = new ConductorController(
                fsm, healthyCheck, urlProvider, () => 'r1',
            );
            await ctrl.start();
            ctrl.startHosting();
            assert.throws(
                () => ctrl.leaveSession(),
                /Cannot leave session from state/,
            );
        });

        it('throws when FSM is in ReadyToHost', async () => {
            const ctrl = new ConductorController(fsm, healthyCheck, urlProvider);
            await ctrl.start();
            assert.throws(
                () => ctrl.leaveSession(),
                /Cannot leave session from state/,
            );
        });
    });
});

