import { describe, it, expect } from "vitest";
import { chatReducer, type ChatState } from "../contexts/ChatContext";
import type { ChatMessage, BrainTree, ThinkingStep, AgentQuestion } from "../types/messages";

// ============================================================
// ChatContext reducer tests
// ============================================================

function makeInitialState(overrides?: Partial<ChatState>): ChatState {
  return {
    messages: [],
    seenIds: new Set(),
    isAIThinking: false,
    brainTree: { thinking: "", agents: {}, phase: "idle", currentAgent: "" },
    currentAction: "",
    thinkingSteps: [],
    agentQuestion: null,
    typingUsers: new Map(),
    hasMoreHistory: true,
    isLoadingHistory: false,
    ...overrides,
  };
}

function makeMsg(id: string, overrides?: Partial<ChatMessage>): ChatMessage {
  return {
    id,
    userId: "u1",
    displayName: "User",
    role: "engineer",
    content: "hello",
    type: "text",
    ts: Date.now() / 1000,
    ...overrides,
  };
}

describe("chatReducer", () => {
  describe("ADD_MESSAGE", () => {
    it("adds a message", () => {
      const state = makeInitialState();
      const msg = makeMsg("m1");
      const next = chatReducer(state, { type: "ADD_MESSAGE", message: msg });
      expect(next.messages).toHaveLength(1);
      expect(next.messages[0].id).toBe("m1");
    });

    it("deduplicates by id", () => {
      const msg = makeMsg("m1");
      const state = makeInitialState({ messages: [msg], seenIds: new Set(["m1"]) });
      const next = chatReducer(state, { type: "ADD_MESSAGE", message: msg });
      expect(next.messages).toHaveLength(1);
    });

    it("does not deduplicate messages with empty id (only id-based dedup)", () => {
      const msg1 = makeMsg("", { content: "hi", ts: 1000 });
      const state = makeInitialState({ messages: [msg1], seenIds: new Set() });
      const msg2 = makeMsg("", { content: "hi", ts: 1000 });
      const next = chatReducer(state, { type: "ADD_MESSAGE", message: msg2 });
      // Empty IDs are not deduplicated — only non-empty IDs are tracked in seenIds
      expect(next.messages).toHaveLength(2);
    });
  });

  describe("ADD_MESSAGES_BATCH", () => {
    it("adds multiple messages (for history)", () => {
      const state = makeInitialState();
      const msgs = [makeMsg("m1", { ts: 1 }), makeMsg("m2", { ts: 2 })];
      const next = chatReducer(state, { type: "ADD_MESSAGES_BATCH", messages: msgs });
      expect(next.messages).toHaveLength(2);
    });

    it("deduplicates within batch", () => {
      const msg = makeMsg("m1");
      const state = makeInitialState({ messages: [msg], seenIds: new Set(["m1"]) });
      const next = chatReducer(state, { type: "ADD_MESSAGES_BATCH", messages: [msg, makeMsg("m2")] });
      expect(next.messages).toHaveLength(2); // m1 (existing) + m2 (new)
    });
  });

  describe("AI_PROGRESS", () => {
    it("sets isAIThinking to true", () => {
      const state = makeInitialState();
      const next = chatReducer(state, {
        type: "AI_PROGRESS",
        event: { phase: "agent", kind: "start", message: "Starting", detail: {} },
      });
      expect(next.isAIThinking).toBe(true);
    });

    it("updates currentAction", () => {
      const state = makeInitialState();
      const next = chatReducer(state, {
        type: "AI_PROGRESS",
        event: { phase: "agent", kind: "tool_call", message: "Reading file", detail: { tool: "read_file" } },
      });
      expect(next.currentAction).toBe("Reading file");
    });
  });

  describe("AI_DONE", () => {
    it("clears thinking state", () => {
      const state = makeInitialState({ isAIThinking: true, currentAction: "Working..." });
      const next = chatReducer(state, { type: "AI_DONE" });
      expect(next.isAIThinking).toBe(false);
      expect(next.currentAction).toBe("");
    });

    it("resets brain tree", () => {
      const state = makeInitialState({ isAIThinking: true });
      const next = chatReducer(state, { type: "AI_DONE" });
      expect(next.brainTree.phase).toBe("idle");
      expect(next.brainTree.agents).toEqual({});
    });
  });

  describe("AGENT_QUESTION / CLEAR_AGENT_QUESTION", () => {
    it("sets agent question", () => {
      const state = makeInitialState();
      const q: AgentQuestion = { sessionId: "s1", question: "Which file?", options: ["a.ts", "b.ts"] };
      const next = chatReducer(state, { type: "AGENT_QUESTION", question: q });
      expect(next.agentQuestion).toEqual(q);
    });

    it("clears agent question", () => {
      const q: AgentQuestion = { sessionId: "s1", question: "Which?" };
      const state = makeInitialState({ agentQuestion: q });
      const next = chatReducer(state, { type: "CLEAR_AGENT_QUESTION" });
      expect(next.agentQuestion).toBeNull();
    });
  });

  describe("SET_TYPING", () => {
    it("adds typing user", () => {
      const state = makeInitialState();
      const next = chatReducer(state, { type: "SET_TYPING", userId: "u2", isTyping: true });
      expect(next.typingUsers.has("u2")).toBe(true);
    });

    it("removes typing user", () => {
      const typing = new Map([["u2", 123]]);
      const state = makeInitialState({ typingUsers: typing });
      const next = chatReducer(state, { type: "SET_TYPING", userId: "u2", isTyping: false });
      expect(next.typingUsers.has("u2")).toBe(false);
    });
  });

  describe("CLEAR_MESSAGES", () => {
    it("resets messages, seenIds, and thinkingSteps", () => {
      const state = makeInitialState({
        messages: [makeMsg("m1")],
        seenIds: new Set(["m1"]),
        thinkingSteps: [{ tool: "read_file", summary: "Read foo.ts" }],
      });
      const next = chatReducer(state, { type: "CLEAR_MESSAGES" });
      expect(next.messages).toHaveLength(0);
      expect(next.seenIds.size).toBe(0);
      expect(next.thinkingSteps).toHaveLength(0);
    });

    it("preserves isAIThinking (not reset by CLEAR_MESSAGES)", () => {
      const state = makeInitialState({ isAIThinking: true });
      const next = chatReducer(state, { type: "CLEAR_MESSAGES" });
      expect(next.isAIThinking).toBe(true); // only AI_DONE resets this
    });
  });

  describe("SET_HISTORY_LOADING / SET_HAS_MORE_HISTORY", () => {
    it("sets loading flag", () => {
      const state = makeInitialState();
      const next = chatReducer(state, { type: "SET_HISTORY_LOADING", loading: true });
      expect(next.isLoadingHistory).toBe(true);
    });

    it("sets hasMore flag", () => {
      const state = makeInitialState();
      const next = chatReducer(state, { type: "SET_HAS_MORE_HISTORY", hasMore: false });
      expect(next.hasMoreHistory).toBe(false);
    });
  });
});
