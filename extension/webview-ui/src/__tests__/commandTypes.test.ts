import { describe, it, expect } from "vitest";

// ============================================================
// Command type contract tests — verify the IncomingCommand and
// OutgoingCommand unions cover all commands used by components.
// These are compile-time checks wrapped as runtime tests.
// ============================================================

// Import the types to ensure they compile
import type { IncomingCommand, OutgoingCommand } from "../types/commands";

describe("OutgoingCommand contract", () => {
  // Each test verifies that a specific command shape is assignable to OutgoingCommand.
  // If a command is missing from the union, TypeScript will catch it at compile time.

  it("session commands", () => {
    const cmds: OutgoingCommand[] = [
      { command: "startSession" },
      { command: "stopSession" },
      { command: "joinSession", inviteUrl: "http://..." },
      { command: "quitChat" },
      { command: "retryConnection" },
      { command: "getConductorState" },
      { command: "getPermissions" },
      { command: "getAutoApplyState" },
      { command: "setAutoApply", enabled: true },
    ];
    expect(cmds).toHaveLength(9);
  });

  it("AI commands", () => {
    const cmds: OutgoingCommand[] = [
      { command: "askAI", roomId: "r1", query: "explain this" },
      { command: "askAI", roomId: "r1", query: "plan", planMode: true },
      { command: "stopAskAI" },
      { command: "agentAnswer", sessionId: "s1", answer: "yes" },
      { command: "setAiModel", modelId: "claude-3" },
    ];
    expect(cmds).toHaveLength(5);
  });

  it("code commands", () => {
    const cmds: OutgoingCommand[] = [
      { command: "navigateToCode", relativePath: "src/foo.ts", startLine: 1 },
      { command: "applyChanges", changeSet: { changes: [] } },
      { command: "viewDiff", changeSet: { changes: [] } },
      { command: "discardChanges" },
    ];
    expect(cmds).toHaveLength(4);
  });

  it("todo commands", () => {
    const cmds: OutgoingCommand[] = [
      { command: "createTodo", title: "Fix bug", roomId: "r1" },
      { command: "updateTodo", todoId: "t1" },
      { command: "loadTodos", roomId: "r1" },
      { command: "deleteTodo", todoId: "t1" },
      { command: "scanWorkspaceTodos" },
      { command: "updateWorkspaceTodo", payload: { filePath: "/a.ts", lineNumber: 1, newTitle: "X", newDescription: "", commentPrefix: "//" } },
    ];
    expect(cmds).toHaveLength(6);
  });

  it("jira commands", () => {
    const cmds: OutgoingCommand[] = [
      { command: "jiraConnect" },
      { command: "jiraDisconnect" },
      { command: "jiraCheckStatus" },
      { command: "jiraCreateIssue", projectKey: "DEV", summary: "Bug", description: "", issueType: "Bug" },
      { command: "jiraGetIssueTypes", projectKey: "DEV" },
      { command: "loadJiraTickets" },
    ];
    expect(cmds).toHaveLength(6);
  });

  it("workspace commands", () => {
    const cmds: OutgoingCommand[] = [
      { command: "setupWorkspaceAndIndex", repoUrl: "https://github.com/org/repo.git", sourceBranch: "main" },
      { command: "rebuildIndex" },
      { command: "fetchRemoteBranches", repoUrl: "https://github.com/org/repo.git" },
      { command: "openConductorWorkspace", roomId: "r1" },
    ];
    expect(cmds).toHaveLength(4);
  });

  it("local session commands", () => {
    const cmds: OutgoingCommand[] = [
      { command: "getLocalSessions" },
      { command: "deleteLocalSession", roomId: "r1" },
      { command: "renameLocalSession", roomId: "r1", displayName: "My Session" },
    ];
    expect(cmds).toHaveLength(3);
  });

  it("summary + code prompt commands", () => {
    const cmds: OutgoingCommand[] = [
      { command: "summarize", query: "distill all", context: {} },
      { command: "generateCodePrompt", decisionSummary: "Do X" },
      { command: "generateCodePromptAndPost", decisionSummary: "Do X", roomId: "r1" },
    ];
    expect(cmds).toHaveLength(3);
  });
});

describe("IncomingCommand contract", () => {
  it("AI progress commands", () => {
    const cmds: IncomingCommand[] = [
      { command: "askAIProgress", phase: "agent", kind: "start", message: "Starting", detail: {} },
      { command: "askAIDone" },
      { command: "askAIDone", error: "timeout", stopped: true },
      { command: "agentQuestion", sessionId: "s1", question: "Which?" },
    ];
    expect(cmds).toHaveLength(4);
  });

  it("todo commands", () => {
    const cmds: IncomingCommand[] = [
      { command: "todosLoaded", todos: [] },
      { command: "todoCreated", todo: { id: "1", title: "X", status: "pending", source: "code" } },
      { command: "todoUpdated", todo: { id: "1", title: "X", status: "done", source: "code" } },
      { command: "todoDeleted", todoId: "1" },
      { command: "workspaceTodosScanned", todos: [] },
      { command: "workspaceTodoUpdated", ok: true, filePath: "/a.ts" },
    ];
    expect(cmds).toHaveLength(6);
  });

  it("index commands", () => {
    const cmds: IncomingCommand[] = [
      { command: "indexRebuildComplete", success: true },
      { command: "indexProgress", payload: { phase: "scanning", filesScanned: 10, totalFiles: 100 } },
      { command: "indexBranchChanged", from: "main", to: "feature/x" },
      { command: "remoteBranchesLoaded", branches: ["main", "develop"] },
      { command: "setupAndIndexComplete", success: true },
    ];
    expect(cmds).toHaveLength(5);
  });

  it("jira commands", () => {
    const cmds: IncomingCommand[] = [
      { command: "jiraConnected", site_url: "https://jira.example.com" },
      { command: "jiraDisconnected" },
      { command: "jiraAuthRequired", authorizeUrl: "https://auth..." },
      { command: "jiraStatus", connected: true, site_url: "https://..." },
      { command: "jiraIssueCreated", key: "DEV-123" },
      { command: "jiraError", error: "auth failed" },
    ];
    expect(cmds).toHaveLength(6);
  });

  it("code change commands", () => {
    const cmds: IncomingCommand[] = [
      { command: "showCurrentChange", currentChange: {}, currentIndex: 0, totalChanges: 1 },
      { command: "allChangesComplete" },
    ];
    expect(cmds).toHaveLength(2);
  });

  it("local session commands", () => {
    const cmds: IncomingCommand[] = [
      { command: "localSessionsList", sessions: [] },
      { command: "localSessionDeleted", roomId: "r1" },
      { command: "localSessionRenamed", roomId: "r1", displayName: "New Name" },
    ];
    expect(cmds).toHaveLength(3);
  });
});
