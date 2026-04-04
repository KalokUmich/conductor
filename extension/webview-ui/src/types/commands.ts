// ============================================================
// postMessage command types — the contract between React and extension
// ============================================================

import type {
  AIProgressEvent,
  AgentQuestion,
  ChangeSet,
  ConductorState,
  Permissions,
  Room,
  Session,
  SSOIdentity,
  ThinkingStep,
  Todo,
} from "./messages";

// --- Extension → WebView (incoming) ---

export type IncomingCommand =
  | { command: "conductorStateChanged"; state: ConductorState; session: Session; ssoIdentity?: SSOIdentity; ssoProvider?: string }
  | { command: "updatePermissions"; permissions: Permissions }
  | { command: "autoApplyState"; enabled: boolean }
  | { command: "endChatConfirmed" }
  | { command: "onlineRoomsList"; rooms: Room[] }
  | { command: "quitRoomsList"; rooms: Room[] }
  // AI
  | { command: "askAIProgress" } & AIProgressEvent
  | { command: "agentQuestion" } & AgentQuestion
  | { command: "askAIDone"; error?: string; stopped?: boolean; thinkingSteps?: ThinkingStep[] }
  // Jira
  | { command: "jiraConnected"; site_url: string }
  | { command: "jiraDisconnected" }
  | { command: "jiraAuthRequired"; authorizeUrl: string }
  | { command: "jiraIssueTypes"; types: Array<{ name: string; id: string }> }
  | { command: "jiraCreateMeta"; priorities?: Array<{ name: string }>; teams?: Array<{ name: string }>; components?: Array<{ name: string }> }
  | { command: "jiraIssueCreated"; key: string; browse_url?: string }
  | { command: "jiraError"; error: string }
  | { command: "jiraSearchResults"; results: unknown[]; query: string }
  // Files
  | { command: "uploadFileResult"; success: boolean; result?: Record<string, unknown>; error?: string }
  | { command: "checkDuplicateFileResult"; duplicate: boolean; existing_file?: string }
  // Code changes
  | { command: "showCurrentChange"; currentChange: unknown; currentIndex: number; totalChanges: number; policyResult?: unknown }
  | { command: "allChangesComplete" }
  // Tool dispatch
  | { command: "tool_response"; requestId: string; tool: string; success: boolean; data: unknown; error: string | null; truncated?: boolean }
  // Todos
  | { command: "todosLoaded"; todos: Todo[] }
  | { command: "todoCreated"; todo: Todo }
  | { command: "todoUpdated"; todo: Todo }
  | { command: "todoDeleted"; todoId: string }
  | { command: "workspaceTodosScanned"; todos: Todo[]; error?: string }
  | { command: "workspaceTodoUpdated"; ok: boolean; filePath: string }
  | { command: "todoDoneConfirmed" }
  // Jira tickets
  | { command: "jiraTicketsLoaded"; tickets: unknown[] }
  // Summary
  | { command: "summarizeResult"; data: { decision_summary?: string; error?: string } }
  | { command: "codePromptResult"; data: { code_prompt?: string; error?: string } }
  // Room settings
  | { command: "roomSettingsSaved"; ok: boolean; error?: string }
  // AI model
  | { command: "setAiModelResult"; data: { success: boolean; active_model?: string; message: string; error?: string } }
  // Index
  | { command: "indexRebuildComplete"; success: boolean; error?: string }
  // SSO
  | { command: "ssoCacheCleared" }
  // History
  | { command: "historyLoaded"; messages: unknown[] }
  // Local messages
  | { command: "localMessagesLoaded"; messages: unknown[] }
  // Backend diagnostics
  | { command: "backendConnectionDiagnosis"; requestId: string; diagnosis: Record<string, unknown> }
  // Stack trace & test failures
  | { command: "stackTraceResolved"; frames: unknown[] }
  | { command: "testFailuresResolved"; failures: unknown[] };

// --- WebView → Extension (outgoing) ---

export type OutgoingCommand =
  // Session
  | { command: "startSession" }
  | { command: "stopSession" }
  | { command: "joinSession"; inviteUrl: string }
  | { command: "leaveSession" }
  | { command: "sessionEnded" }
  | { command: "confirmEndChat" }
  | { command: "quitChat" }
  | { command: "retryConnection" }
  | { command: "getConductorState" }
  | { command: "getPermissions" }
  | { command: "getAutoApplyState" }
  | { command: "setAutoApply"; enabled: boolean }
  // Room
  | { command: "copyInviteLink" }
  | { command: "getOnlineRooms" }
  | { command: "getQuitRooms" }
  | { command: "removeQuitRoom"; roomId: string }
  | { command: "rejoinRoom"; roomId: string }
  | { command: "getRoomSettings"; roomId: string }
  | { command: "saveRoomSettings"; roomId: string; settings: Record<string, unknown> }
  // AI
  | { command: "askAI"; roomId: string; query: string; planMode?: boolean; codeContext?: Record<string, unknown> }
  | { command: "stopAskAI" }
  | { command: "getAiStatus" }
  | { command: "setAiModel"; modelId: string }
  | { command: "setClassifier"; classifier: string }
  | { command: "setExplorer"; explorer: string }
  | { command: "agentAnswer"; sessionId: string; answer: string }
  // Code
  | { command: "getCodeSnippet"; filePath: string; startLine: number; endLine: number }
  | { command: "navigateToCode"; relativePath: string; startLine: number; endLine?: number }
  | { command: "generateChanges"; filePath: string }
  | { command: "applyChanges"; changeSet: ChangeSet }
  | { command: "viewDiff"; changeSet: ChangeSet }
  | { command: "discardChanges" }
  // Files
  | { command: "uploadFile"; roomId: string; userId: string; displayName: string; fileData: string; fileName: string; mimeType: string; caption?: string }
  | { command: "checkDuplicateFile"; fileName: string }
  | { command: "downloadFile"; fileId: string; fileName: string; downloadUrl: string }
  // Messages
  | { command: "loadLocalMessages"; roomId: string }
  | { command: "saveLocalMessages"; roomId: string; messages: unknown[] }
  | { command: "clearLocalMessages"; roomId: string }
  | { command: "loadHistory"; roomId: string }
  // Tool dispatch
  | { command: "tool_request"; requestId: string; tool: string; params: Record<string, unknown>; workspace?: string }
  | { command: "tool_response"; requestId: string; tool: string; success: boolean; data: unknown; error: string | null; truncated?: boolean }
  // Summary
  | { command: "summarize"; query: string; context: unknown }
  | { command: "generateCodePrompt"; decisionSummary: string }
  | { command: "generateCodePromptAndPost"; decisionSummary: string; roomId: string }
  | { command: "generateCodePromptFromItemsAndPost"; items: unknown[]; roomId: string }
  // Jira
  | { command: "jiraCheckStatus" }
  | { command: "jiraConnect" }
  | { command: "jiraDisconnect" }
  | { command: "jiraCreateIssue"; projectKey: string; summary: string; description: string; issueType: string; priority?: string; team?: string; components?: string[] }
  | { command: "jiraGetIssueTypes"; projectKey: string }
  | { command: "jiraGetCreateMeta"; projectKey?: string }
  | { command: "jiraSearch"; query: string }
  | { command: "loadJiraTickets" }
  // Todos
  | { command: "createTodo"; title: string; description?: string; roomId: string }
  | { command: "updateTodo"; todoId: string; [key: string]: unknown }
  | { command: "loadTodos"; roomId: string }
  | { command: "deleteTodo"; todoId: string }
  | { command: "scanWorkspaceTodos" }
  | { command: "updateWorkspaceTodo"; filePath: string; lineNumber: number; updates: Record<string, unknown> }
  | { command: "startTaskFromTodo"; todoId: string; roomId: string }
  | { command: "confirmTodoDone"; todoId: string; filePath: string }
  // SSO
  | { command: "ssoLogin"; provider: string }
  | { command: "ssoCancel" }
  | { command: "ssoClearCache" }
  // Workspace
  | { command: "setupLocalWorkspace" }
  | { command: "setupWorkspaceAndIndex"; workspacePath: string }
  | { command: "rebuildIndex" }
  | { command: "fetchRemoteBranches" }
  | { command: "openConductorWorkspace"; roomId: string }
  // External
  | { command: "openExternal"; url: string }
  | { command: "showWorkflow" }
  | { command: "alert"; text: string }
  // Stack trace & test
  | { command: "shareStackTrace"; stackTrace: string }
  | { command: "shareTestOutput"; output: string; framework?: string }
  | { command: "shareTestFailures"; failures: unknown[] }
  // Diagnostics
  | { command: "diagnoseBackendConnection"; requestId: string };
