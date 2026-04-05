import { describe, it, expect } from "vitest";
import {
  classifyMessage,
  parseMessageData,
  hasRenderableContent,
  SKIP_TYPES,
  SPECIAL_TYPES,
} from "../utils/wsMessageParser";

// ============================================================
// WebSocket message parser tests
// ============================================================

describe("classifyMessage", () => {
  it('classifies "connected" as special', () => {
    expect(classifyMessage("connected", {})).toBe("special");
  });

  it('classifies "typing" as special', () => {
    expect(classifyMessage("typing", {})).toBe("special");
  });

  it('classifies "user_joined" as special', () => {
    expect(classifyMessage("user_joined", {})).toBe("special");
  });

  it('classifies "user_left" as special', () => {
    expect(classifyMessage("user_left", {})).toBe("special");
  });

  it('classifies "session_ended" as special', () => {
    expect(classifyMessage("session_ended", {})).toBe("special");
  });

  it('classifies "lead_changed" as special', () => {
    expect(classifyMessage("lead_changed", {})).toBe("special");
  });

  it('classifies "read_receipt" as special', () => {
    expect(classifyMessage("read_receipt", {})).toBe("special");
  });

  it('classifies "tool_request" as special', () => {
    expect(classifyMessage("tool_request", {})).toBe("special");
  });

  it('classifies "role_restored" as special', () => {
    expect(classifyMessage("role_restored", {})).toBe("special");
  });

  it('classifies "error" as skip', () => {
    expect(classifyMessage("error", {})).toBe("skip");
  });

  it('classifies "lead_changed_ack" as skip', () => {
    expect(classifyMessage("lead_changed_ack", {})).toBe("skip");
  });

  it('classifies "settings_updated" as skip', () => {
    expect(classifyMessage("settings_updated", {})).toBe("skip");
  });

  it('classifies "quit_confirmed" as skip', () => {
    expect(classifyMessage("quit_confirmed", {})).toBe("skip");
  });

  it("classifies tool_response by command field", () => {
    expect(classifyMessage("", { command: "tool_response" })).toBe("tool_response");
    expect(classifyMessage("anything", { command: "tool_response" })).toBe("tool_response");
  });

  it('classifies "text" as chat', () => {
    expect(classifyMessage("text", {})).toBe("chat");
  });

  it('classifies "code_snippet" as chat', () => {
    expect(classifyMessage("code_snippet", {})).toBe("chat");
  });

  it('classifies "ai_answer" as chat', () => {
    expect(classifyMessage("ai_answer", {})).toBe("chat");
  });

  it('classifies "file" as chat', () => {
    expect(classifyMessage("file", {})).toBe("chat");
  });

  it('classifies unknown types as chat', () => {
    expect(classifyMessage("custom_type", {})).toBe("chat");
  });
});

describe("parseMessageData", () => {
  it("parses a basic text message", () => {
    const msg = parseMessageData({
      id: "m1",
      userId: "u1",
      displayName: "Alice",
      role: "engineer",
      content: "Hello world",
      type: "text",
      ts: 1000,
    });
    expect(msg.id).toBe("m1");
    expect(msg.userId).toBe("u1");
    expect(msg.displayName).toBe("Alice");
    expect(msg.content).toBe("Hello world");
    expect(msg.type).toBe("text");
    expect(msg.ts).toBe(1000);
  });

  it("falls back to sender field for userId", () => {
    const msg = parseMessageData({ sender: "s1" });
    expect(msg.userId).toBe("s1");
    expect(msg.sender).toBe("s1");
  });

  it("generates id if missing", () => {
    const msg = parseMessageData({});
    expect(msg.id).toMatch(/^msg-/);
  });

  it("defaults type to text", () => {
    const msg = parseMessageData({});
    expect(msg.type).toBe("text");
  });

  it("parses code snippet fields", () => {
    const snippet = { code: "x=1", filename: "a.py", startLine: 1, endLine: 1, language: "python" };
    const msg = parseMessageData({ type: "code_snippet", codeSnippet: snippet });
    expect(msg.type).toBe("code_snippet");
    expect(msg.codeSnippet).toEqual(snippet);
  });

  it("parses AI answer fields", () => {
    const msg = parseMessageData({
      type: "ai_answer",
      answer: "The function does X",
      thinkingSteps: [{ tool: "read_file", summary: "Read foo.ts" }],
    });
    expect(msg.type).toBe("ai_answer");
    expect(msg.answer).toBe("The function does X");
    expect(msg.thinkingSteps).toHaveLength(1);
  });

  it("parses AI summary fields", () => {
    const msg = parseMessageData({ type: "ai_summary", summary: "Decision: do X" });
    expect(msg.summary).toBe("Decision: do X");
  });

  it("parses code prompt from both field names", () => {
    expect(parseMessageData({ codePrompt: "prompt A" }).codePrompt).toBe("prompt A");
    expect(parseMessageData({ code_prompt: "prompt B" }).codePrompt).toBe("prompt B");
  });

  it("parses file message fields", () => {
    const msg = parseMessageData({
      type: "file",
      fileId: "f1",
      originalFilename: "doc.pdf",
      mimeType: "application/pdf",
      sizeBytes: 1024,
      downloadUrl: "https://...",
      caption: "Check this out",
    });
    expect(msg.fileId).toBe("f1");
    expect(msg.originalFilename).toBe("doc.pdf");
    expect(msg.sizeBytes).toBe(1024);
    expect(msg.caption).toBe("Check this out");
  });

  it("parses stack trace fields", () => {
    const trace = { rawTrace: "Error...", frames: [] };
    const msg = parseMessageData({ type: "stack_trace", stackTrace: trace });
    expect(msg.stackTrace).toEqual(trace);
  });

  it("parses test failures fields", () => {
    const failures = { framework: "pytest", totalFailed: 2, tests: [] };
    const msg = parseMessageData({ type: "test_failures", testFailures: failures });
    expect(msg.testFailures).toEqual(failures);
  });

  it("parses aiMeta", () => {
    const meta = { model: "claude-3", tokensIn: 100, tokensOut: 50 };
    const msg = parseMessageData({ aiMeta: meta });
    expect(msg.aiMeta).toEqual(meta);
  });
});

describe("hasRenderableContent", () => {
  it("returns true for text content", () => {
    expect(hasRenderableContent({ content: "hello" })).toBe(true);
  });

  it("returns true for AI answer", () => {
    expect(hasRenderableContent({ answer: "yes" })).toBe(true);
  });

  it("returns true for summary", () => {
    expect(hasRenderableContent({ summary: "Decision X" })).toBe(true);
  });

  it("returns true for code prompt", () => {
    expect(hasRenderableContent({ codePrompt: "do X" })).toBe(true);
    expect(hasRenderableContent({ code_prompt: "do Y" })).toBe(true);
  });

  it("returns true for code snippet", () => {
    expect(hasRenderableContent({ codeSnippet: {} })).toBe(true);
  });

  it("returns true for file", () => {
    expect(hasRenderableContent({ fileId: "f1" })).toBe(true);
  });

  it("returns true for stack trace", () => {
    expect(hasRenderableContent({ stackTrace: {} })).toBe(true);
  });

  it("returns true for test failures", () => {
    expect(hasRenderableContent({ testFailures: {} })).toBe(true);
  });

  it("returns false for empty data", () => {
    expect(hasRenderableContent({})).toBe(false);
  });

  it("returns false for protocol-only data", () => {
    expect(hasRenderableContent({ type: "typing", userId: "u1" })).toBe(false);
  });
});

describe("SKIP_TYPES / SPECIAL_TYPES", () => {
  it("SKIP_TYPES and SPECIAL_TYPES have no overlap", () => {
    SKIP_TYPES.forEach((t) => {
      expect(SPECIAL_TYPES.has(t)).toBe(false);
    });
  });

  it("SKIP_TYPES contains expected entries", () => {
    expect(SKIP_TYPES.has("error")).toBe(true);
    expect(SKIP_TYPES.has("lead_changed_ack")).toBe(true);
    expect(SKIP_TYPES.has("settings_updated")).toBe(true);
    expect(SKIP_TYPES.has("quit_confirmed")).toBe(true);
    expect(SKIP_TYPES.has("end_session_blocked")).toBe(true);
    expect(SKIP_TYPES.has("history_cleared")).toBe(true);
  });

  it("SPECIAL_TYPES contains expected entries", () => {
    expect(SPECIAL_TYPES.has("connected")).toBe(true);
    expect(SPECIAL_TYPES.has("typing")).toBe(true);
    expect(SPECIAL_TYPES.has("user_joined")).toBe(true);
    expect(SPECIAL_TYPES.has("user_left")).toBe(true);
    expect(SPECIAL_TYPES.has("session_ended")).toBe(true);
    expect(SPECIAL_TYPES.has("lead_changed")).toBe(true);
    expect(SPECIAL_TYPES.has("read_receipt")).toBe(true);
    expect(SPECIAL_TYPES.has("tool_request")).toBe(true);
    expect(SPECIAL_TYPES.has("role_restored")).toBe(true);
  });
});
