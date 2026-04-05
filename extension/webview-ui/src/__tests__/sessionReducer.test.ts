import { describe, it, expect } from "vitest";
import { sessionReducer, type SessionState } from "../contexts/SessionContext";

// ============================================================
// SessionContext reducer tests
// ============================================================

function makeInitialState(overrides?: Partial<SessionState>): SessionState {
  return {
    conductorState: "Idle",
    session: null,
    permissions: { sessionRole: "none" },
    ssoIdentity: null,
    ssoProvider: null,
    enabledSSOProviders: [],
    autoApplyEnabled: false,
    users: new Map(),
    isAIBusy: false,
    ssoUIState: "idle",
    ssoPending: null,
    ...overrides,
  };
}

describe("sessionReducer", () => {
  describe("SET_CONDUCTOR_STATE", () => {
    it("updates conductor state", () => {
      const state = makeInitialState();
      const next = sessionReducer(state, { type: "SET_CONDUCTOR_STATE", state: "ReadyToHost" });
      expect(next.conductorState).toBe("ReadyToHost");
    });

    it("sets session when provided", () => {
      const state = makeInitialState();
      const session = { roomId: "r1", hostId: "h1", userId: "u1", createdAt: 1, backendUrl: "http://localhost" };
      const next = sessionReducer(state, { type: "SET_CONDUCTOR_STATE", state: "Hosting", session });
      expect(next.session).toEqual(session);
    });

    it("sets ssoIdentity when provided (ssoUIState unchanged by SET_CONDUCTOR_STATE)", () => {
      const state = makeInitialState();
      const identity = { email: "user@test.com", provider: "google" };
      const next = sessionReducer(state, { type: "SET_CONDUCTOR_STATE", state: "ReadyToHost", ssoIdentity: identity });
      expect(next.ssoIdentity).toEqual(identity);
      // SET_CONDUCTOR_STATE sets identity but doesn't change ssoUIState — SSO_DONE does that
      expect(next.ssoUIState).toBe("idle");
    });
  });

  describe("SET_PERMISSIONS", () => {
    it("updates permissions", () => {
      const state = makeInitialState();
      const next = sessionReducer(state, { type: "SET_PERMISSIONS", permissions: { sessionRole: "host" } });
      expect(next.permissions.sessionRole).toBe("host");
    });
  });

  describe("SET_AUTO_APPLY", () => {
    it("toggles auto-apply", () => {
      const state = makeInitialState();
      const next = sessionReducer(state, { type: "SET_AUTO_APPLY", enabled: true });
      expect(next.autoApplyEnabled).toBe(true);
      const next2 = sessionReducer(next, { type: "SET_AUTO_APPLY", enabled: false });
      expect(next2.autoApplyEnabled).toBe(false);
    });
  });

  describe("SET_USERS", () => {
    it("replaces user map", () => {
      const state = makeInitialState();
      const users = new Map([["u1", { displayName: "Alice", role: "host", avatarColor: 1 }]]);
      const next = sessionReducer(state, { type: "SET_USERS", users });
      expect(next.users.size).toBe(1);
      expect(next.users.get("u1")?.displayName).toBe("Alice");
    });
  });

  describe("UPDATE_USER / REMOVE_USER", () => {
    it("adds or updates a user", () => {
      const state = makeInitialState();
      const next = sessionReducer(state, { type: "UPDATE_USER", userId: "u2", info: { displayName: "Bob", role: "engineer", avatarColor: 2 } });
      expect(next.users.get("u2")?.displayName).toBe("Bob");
    });

    it("removes a user", () => {
      const users = new Map([["u1", { displayName: "Alice", role: "host", avatarColor: 1 }]]);
      const state = makeInitialState({ users });
      const next = sessionReducer(state, { type: "REMOVE_USER", userId: "u1" });
      expect(next.users.has("u1")).toBe(false);
    });
  });

  describe("SSO flow", () => {
    it("SSO_PENDING sets pending state", () => {
      const state = makeInitialState();
      const next = sessionReducer(state, { type: "SSO_PENDING", userCode: "ABC123", provider: "aws" });
      expect(next.ssoUIState).toBe("pending");
      expect(next.ssoPending?.userCode).toBe("ABC123");
    });

    it("SSO_DONE sets done state + identity", () => {
      const state = makeInitialState({ ssoUIState: "pending" });
      const identity = { email: "user@test.com", provider: "google" };
      const next = sessionReducer(state, { type: "SSO_DONE", identity, provider: "google" });
      expect(next.ssoUIState).toBe("done");
      expect(next.ssoIdentity).toEqual(identity);
      expect(next.ssoProvider).toBe("google");
      expect(next.ssoPending).toBeNull();
    });

    it("SSO_CLEARED resets to idle", () => {
      const identity = { email: "user@test.com", provider: "google" };
      const state = makeInitialState({ ssoUIState: "done", ssoIdentity: identity });
      const next = sessionReducer(state, { type: "SSO_CLEARED" });
      expect(next.ssoUIState).toBe("idle");
      expect(next.ssoIdentity).toBeNull();
    });
  });

  describe("RESET_SESSION", () => {
    it("clears session, users, AI state", () => {
      const users = new Map([["u1", { displayName: "Alice", role: "host", avatarColor: 1 }]]);
      const state = makeInitialState({
        conductorState: "Hosting",
        session: { roomId: "r1", hostId: "h1", userId: "u1", createdAt: 1, backendUrl: "http://localhost" },
        users,
        isAIBusy: true,
      });
      const next = sessionReducer(state, { type: "RESET_SESSION" });
      expect(next.session).toBeNull();
      expect(next.users.size).toBe(0);
      expect(next.isAIBusy).toBe(false);
    });
  });
});
