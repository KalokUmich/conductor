import type { ChatMessage } from "../types/messages";

// ============================================================
// WebSocket message parsing — extracted for testability
// ============================================================

/** Message types that should be silently skipped (protocol noise, not renderable). */
export const SKIP_TYPES = new Set([
  "lead_changed_ack", "settings_updated",
  "error", "quit_confirmed", "end_session_blocked", "history_cleared",
]);

/** Message types that are handled as special events (not regular chat messages). */
export const SPECIAL_TYPES = new Set([
  "connected", "typing", "user_joined", "user_left",
  "session_ended", "lead_changed", "read_receipt",
  "tool_request", "role_restored",
]);

/**
 * Classify a WebSocket message by its type.
 * Returns the routing category.
 */
export function classifyMessage(type: string, data: Record<string, unknown>): "special" | "skip" | "chat" | "tool_response" {
  if (data.command === "tool_response") return "tool_response";
  if (SPECIAL_TYPES.has(type)) return "special";
  if (SKIP_TYPES.has(type)) return "skip";
  return "chat";
}

/**
 * Parse raw WebSocket data into a typed ChatMessage.
 * Handles all message fields: text, code_snippet, AI, file, stack_trace, etc.
 */
export function parseMessageData(data: Record<string, unknown>): ChatMessage {
  const userId = (data.userId as string) || (data.sender as string) || "";
  return {
    id: (data.id as string) || `msg-${Date.now()}-${Math.random()}`,
    userId,
    sender: userId,
    displayName: (data.displayName as string) || "Unknown",
    role: (data.role as ChatMessage["role"]) || "engineer",
    content: (data.content as string) || "",
    type: ((data.type as string) || "text") as ChatMessage["type"],
    ts: (data.ts as number) || Date.now() / 1000,
    identitySource: data.identitySource as string,
    metadata: data.metadata as Record<string, unknown>,
    codeSnippet: data.codeSnippet as ChatMessage["codeSnippet"],
    fileId: data.fileId as string,
    originalFilename: data.originalFilename as string,
    fileType: data.fileType as string,
    mimeType: data.mimeType as string,
    sizeBytes: data.sizeBytes as number,
    downloadUrl: data.downloadUrl as string,
    caption: data.caption as string,
    answer: data.answer as string,
    summary: data.summary as string,
    codePrompt: (data.codePrompt as string) || (data.code_prompt as string),
    aiMeta: data.aiMeta as ChatMessage["aiMeta"],
    thinkingSteps: data.thinkingSteps as ChatMessage["thinkingSteps"],
    stackTrace: data.stackTrace as ChatMessage["stackTrace"],
    testFailures: data.testFailures as ChatMessage["testFailures"],
  };
}

/**
 * Check if a message has renderable content (not just protocol noise).
 */
export function hasRenderableContent(data: Record<string, unknown>): boolean {
  return !!(
    data.content ||
    data.answer ||
    data.summary ||
    data.codePrompt ||
    data.code_prompt ||
    data.codeSnippet ||
    data.fileId ||
    data.stackTrace ||
    data.testFailures
  );
}
