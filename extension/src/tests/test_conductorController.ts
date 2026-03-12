/**
 * Legacy test file — migrated to the new DI-based ConductorController API.
 *
 * The original version used Jest + VS Code stubs (old constructor signature).
 * This file has been rewritten to use `node:test` to match the project's
 * current testing approach.  The companion file `conductorController.test.ts`
 * contains the authoritative test suite.
 *
 * This file compiles and runs cleanly but is intentionally minimal to avoid
 * duplicating tests already covered in `conductorController.test.ts`.
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

const BACKEND_URL = 'http://localhost:8000';
const healthyCheck  = async (_url: string): Promise<boolean> => true;
const unhealthyCheck = async (_url: string): Promise<boolean> => false;
const urlProvider   = (): string => BACKEND_URL;

function makeController(healthy = true) {
    return new ConductorController(
        new ConductorStateMachine(),
        healthy ? healthyCheck : unhealthyCheck,
        urlProvider,
    );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('ConductorController (legacy test file — DI API)', () => {
    let fsm: ConductorStateMachine;

    beforeEach(() => {
        fsm = new ConductorStateMachine();
    });

    it('starts in Idle state before start() is called', () => {
        const ctrl = new ConductorController(fsm, healthyCheck, urlProvider);
        assert.equal(ctrl.getState(), ConductorState.Idle);
    });

    it('start() → ReadyToHost when backend is healthy', async () => {
        const ctrl = makeController(true);
        const state = await ctrl.start();
        assert.equal(state, ConductorState.ReadyToHost);
    });

    it('start() → BackendDisconnected when backend is unhealthy', async () => {
        const ctrl = makeController(false);
        const state = await ctrl.start();
        assert.equal(state, ConductorState.BackendDisconnected);
    });

    it('startHosting() transitions ReadyToHost → Hosting', async () => {
        const ctrl = makeController(true);
        await ctrl.start();
        ctrl.startHosting();
        assert.equal(ctrl.getState(), ConductorState.Hosting);
    });

    it('stopHosting() transitions Hosting → ReadyToHost', async () => {
        const ctrl = makeController(true);
        await ctrl.start();
        ctrl.startHosting();
        ctrl.stopHosting();
        assert.equal(ctrl.getState(), ConductorState.ReadyToHost);
    });

    it('onStateChange fires on each FSM transition', async () => {
        const ctrl = makeController(true);
        const seen: ConductorState[] = [];
        ctrl.onStateChange((_prev, next) => seen.push(next));
        await ctrl.start();
        assert.ok(seen.includes(ConductorState.ReadyToHost));
    });

    it('canTransition returns true for valid event and false for invalid', async () => {
        const ctrl = makeController(true);
        await ctrl.start();
        assert.equal(ctrl.canTransition(ConductorEvent.START_HOSTING), true);
        assert.equal(ctrl.canTransition(ConductorEvent.JOIN_SUCCEEDED), false);
    });

    it('startJoining() parses invite URL and transitions to Joining', async () => {
        const ctrl = makeController(true);
        await ctrl.start();
        const invite = ctrl.startJoining(`${BACKEND_URL}?roomId=room-42`);
        assert.equal(invite.roomId, 'room-42');
        assert.equal(ctrl.getState(), ConductorState.Joining);
    });
});
